from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends, HTTPException, status
import secrets
import azure.functions as func
from azure.devops.v7_0.work_item_tracking.work_item_tracking_client import WorkItemTrackingClient
from azure.devops.v7_0.work_item_tracking.models import WorkItemQueryResult
import logging
import os
import base64
from typing import List, Optional
from pydantic import BaseModel, Field, HttpUrl
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
from bs4 import BeautifulSoup
import html
import fastapi
from loguru import logger 
from dotenv import load_dotenv
load_dotenv()

security = HTTPBasic()
API_USERNAME = os.getenv("API_USERNAME", "admin")
API_PASSWORD = os.getenv("API_PASSWORD", "password")

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, API_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, API_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


WIQL_TEMPLATE = """
SELECT
   {selected_fields}
FROM workitems
WHERE
   [System.TeamProject] = '{team}'
   AND [System.WorkItemType] IN ({workitem_types})
   AND [System.State] <> ''
   AND [System.CreatedDate] >= @Today - 30
   AND [System.State] NOT IN ('Completed','Canceled','Done','Resolved','Closed')
   {extra_filters}
ORDER BY
   [System.ChangedDate] DESC
"""

allowed_fields_default = {
    'System.Id': {'name': 'System.Id', 'title': 'ID'},
    'System.WorkItemType': {'name': 'System.WorkItemType', 'title': 'Work Item Type'},
    'System.Title': {'name': 'System.Title', 'title': 'Title'},
    'System.State': {'name': 'System.State', 'title': 'State'},
    'Microsoft.VSTS.Scheduling.Effort': {'name': 'Microsoft.VSTS.Scheduling.Effort', 'title': 'Effort'},
    'Microsoft.VSTS.Common.BusinessValue': {'name': 'Microsoft.VSTS.Common.BusinessValue', 'title': 'Business Value'},
    'Microsoft.VSTS.Common.ValueArea': {'name': 'Microsoft.VSTS.Common.ValueArea', 'title': 'Qual o problema ou dor do cliente a ser resolvido'},
    'System.Tags': {'name': 'System.Tags', 'title': 'Tags'},
    'System.Description': {'name': 'System.Description', 'title': 'Description'},
    'Custom.TipodeIniciativa': {'name': 'Custom.TipodeIniciativa', 'title': 'Tipo de Iniciativa'},
    'Custom.05912272-678c-4f26-8aa7-72eba9b2a56a': {'name': 'Custom.05912272-678c-4f26-8aa7-72eba9b2a56a', 'title': 'Projeto Estratégico'},
    'Custom.MetaEloassociada': {'name': 'Custom.MetaEloassociada', 'title': 'Meta Elo associada'},
    'Custom.MetaEloAssociada2': {'name': 'Custom.MetaEloAssociada2', 'title': 'Meta Elo Associada 2'},
    'Custom.Metadiretoriaassociada': {'name': 'Custom.Metadiretoriaassociada', 'title': 'Meta diretoria associada'},
    'Custom.GanhoQuantitativo': {'name': 'Custom.GanhoQuantitativo', 'title': 'Ganho Quantitativo'}
}


class AzureDevOpsConfig(BaseModel):
    """Configuration for Azure DevOps connection."""
    base_url: HttpUrl = Field(..., description="Base WIQL API URL")
    organization: str = Field(..., description="Azure DevOps Organization name")
    team: str = Field(..., description="Azure DevOps Team name")
    project: str = Field(..., description="Azure DevOps Project name")
    personal_access_token: str = Field(..., description="Azure DevOps Personal Access Token")
    top: int = Field(default=50, description="Max number of work items to return")


class WIQLQueryParams(BaseModel):
    """Parameters for the WIQL query."""
    days_back: int = Field(default=60, description="Number of days back from today")
    excluded_states: List[str] = Field(default_factory=lambda: ["Completed", "Canceled", "Done", "Closed", "Resolved"],
                                       description="List of states to exclude from the query. If empty, defaults to common closed states.",
                                       examples=[["Completed", "Canceled", "Done", "Closed", "Resolved"]],)
    area_paths: List[str] = Field(default_factory=list,
                                  description="List of area paths to filter by. If empty, no area path filter is applied.",
                                  examples=[["Elo\\Meios de Pagamento e Anti Fraude\\Meios de Pagamento\\Credenciais de Pagamentos",
                                            "Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Compra Online"]])
    # Value based filters for the WIQL query. Format: {'field_name': ['values']}. Checks if work_item[field_name] is in values list.
    value_filters: dict[str, List[str]] = Field(default_factory=dict,
                                                description="Value based filters for the WIQL query. Format: {'field_name': ['values']}. Checks if work_item[field_name] is in values list.",
                                                examples=[{"System.State": ["Active", "New"]}])
    keyword_filters: dict[str, str] = Field(default_factory=dict,
                                            description="Keyword based filters for the WIQL query. Format: {'field_name': 'keyword'}. Checks if work_item[field_name] contains keyword.",
                                            examples=[{"System.Title": "fraude", "System.Description": "Lyra"}])
    query: Optional[str] = Field(default=None, description="Custom WIQL query. If provided, it overrides other parameters.")
    allowed_fields: dict[str, dict[str, str]] = Field(
        default=allowed_fields_default,
        description="Dictionary of allowed fields with their names and titles. Used to build the WIQL query.",
        title="Allowed Fields"
    )


class WIQLRequestBody(BaseModel):
    """Request body for the WIQL query endpoint."""
    pat: str = Field(..., description="Personal Access Token for Azure DevOps")
    top: int = Field(default=50, description="Max number of work items to return")
    parameters: WIQLQueryParams = Field(default_factory=lambda: WIQLQueryParams())


class Response(BaseModel):
    """Response model for the WIQL query endpoint."""
    # work_items: List[str] = Field(..., description="List of work items returned by the WIQL query") 
    header: List[str] = Field(..., description="List of column titles for the work items")
    values: List[List[str]] = Field(..., description="List of work item rows, each as a list of values")


def get_ado_client(config: AzureDevOpsConfig) -> WorkItemTrackingClient:
    credentials = BasicAuthentication('', config.personal_access_token)
    connection = Connection(base_url=f'{config.base_url}/{config.organization}',
                            creds=credentials)
    return connection.clients.get_work_item_tracking_client()


def get_work_items(params: WIQLQueryParams, config: AzureDevOpsConfig) -> List[List[str]]:
    """
    Executes the WIQL query and retrieves work items from Azure DevOps Boards.
    """
    client = get_ado_client(config)
    wiql = build_query(params, config, query=params.query)
    result: WorkItemQueryResult = client.query_by_wiql({'query': wiql}, top=config.top, time_precision=True)

    work_items = getattr(result, 'work_items', None)
    ids = []
    if work_items:
        logger.info(work_items[0] if work_items else "No work items found.")
        ids = [item.id for item in work_items]
    elif hasattr(result, 'work_item_relations') and result.work_item_relations:
        logger.info("Using work_item_relations for IDs.")
        ids = [rel.target.id for rel in result.work_item_relations if hasattr(rel, 'target') and hasattr(rel.target, 'id')]
    else:
        logger.info("No work items or work item relations found.")
        return []
    if not ids:
        logger.info("No work items found.")
        return []
    
    batch_size = 199
    values = []
    sorted_fields = sorted(params.allowed_fields.keys())
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i:i+batch_size]
        batch_items = client.get_work_items(batch_ids, expand='all')
        for work_item in batch_items:
            wi: List[str] = build_work_item(work_item, sorted_fields=sorted_fields)
            if wi:
                values.append(wi)
    logger.info(f"Retrieved {len(values)} work items after processing batches.")
    return values


def build_query(params: WIQLQueryParams, config: AzureDevOpsConfig, query: Optional[str]) -> str:
    """
    Builds the WIQL query string with given parameters.
    """
    team = config.team
    
    if not query:
        clauses = []

        excluded_states = params.excluded_states or ["Completed", "Canceled", "Done", "Closed", "Resolved"]
        if excluded_states:
            excluded_states_str = ", ".join(f"'{state}'" for state in excluded_states)
            clauses.append(f"[System.State] NOT IN ({excluded_states_str})")

        area_path_filter = ""
        area_paths = params.area_paths
        if not area_paths:
            area_paths = [
                'Elo\\Meios de Pagamento e Anti Fraude\\Meios de Pagamento\\Credenciais de Pagamentos',
                'Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Compra Online',
                'Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Demandas a Prev Fraude',
                'Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Transacional',
                'Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Validação Cadastral',
                'Elo\\Meios de Pagamento e Anti Fraude\\Anti-Fraude\\Consórcio combate a fraudes'
            ]
        if len(params.area_paths) == 1:
            area_path = params.area_paths[0].replace("'", "\\'")
            area_path_filter = f"AND ([System.AreaPath] UNDER '{area_path}') "
        else:
            or_conditions = " OR ".join(
                "[System.AreaPath] UNDER '"+ap.replace("'", "\\'")+"'" for ap in params.area_paths
            )
            area_path_filter = f"AND ( {or_conditions} ) "
        clauses.append(area_path_filter)

        value_filters = params.value_filters
        keyword_filters = params.keyword_filters

        clauses = []
        if value_filters:
            for field, values in value_filters.items():
                if values:
                    values_list = [f"'{v}'" for v in values]
                    clauses.append(f"[{field}] IN ({','.join(values_list)})")

        if keyword_filters:
            for field, keyword in keyword_filters.items():
                clauses.append(f"[{field}] CONTAINS '{keyword}'")

        extra_filters = ""
        if clauses:
            extra_filters = " AND " + " AND ".join(clauses)

        sorted_fields = sorted(params.allowed_fields.keys())
        selected_fields = ", ".join(sorted_fields)
        workitem_types = ["Iniciativa E2E"]
        
        query = WIQL_TEMPLATE.format(
            selected_fields=selected_fields,
            workitem_types=", ".join(f"'{wi}'" for wi in workitem_types),
            extra_filters=extra_filters,
            team=team or 'Elo'
        )
    logger.info(f"Built WIQL query: {query}")
    return query


def get_auth_header(token: str) -> dict[str, str]:
    """
    Returns the HTTP Authorization header for Azure DevOps using Basic Auth.
    Username is empty, password is the PAT.
    """
    credentials = f":{token}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("utf-8")
    return {"Authorization": f"Basic {encoded_credentials}"}


def build_work_item(work_item, sorted_fields: List[str]) -> List[str]:
    """
    Builds a string representation of the work item.
    """
    columns = []
    for f in sorted_fields:
        if f in work_item.fields and work_item.fields[f]:
            value = work_item.fields[f]
            if f == "System.Description":
                try:
                    value = work_item.fields[f].encode().decode("unicode_escape")
                except:
                    pass
                soup = BeautifulSoup(value, "html.parser")
                value = html.unescape(soup.get_text(separator=" "))
        else:
            value = ""
        columns.append(str(value))
            
    return columns
    

fapi_app = fastapi.FastAPI(
    title="Azure DevOps Boards WIQL API",
    description="API para consultar Azure DevOps Boards usando WIQL.",
    version="1.0.0",
    contact={
        "name": "Thiago Salles",
        "email": "thiagosalles@microsoft.com",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
    openapi_tags=[
        {
            "name": "Azure Boards WIQL",
            "description": "Endpoints para consultar Azure DevOps Boards usando WIQL.",
        }
    ],
    openapi_version="3.0.1"
)

@fapi_app.post(
    "/v1/wiql",
    operation_id="WIQLQuery",
    response_model=Response,
    tags=["Azure Boards WIQL"],
    summary="Executa uma consulta WIQL",
    description="Executa uma consulta WIQL no Azure DevOps Boards e retorna os itens de trabalho correspondentes."
)
def azure_board_query(req: WIQLRequestBody, username: str = Depends(authenticate)) -> Response:
    logging.info('Python HTTP trigger function processed a request.')
    config = AzureDevOpsConfig(
        base_url=os.getenv("ADO_BASE_URL", "https://dev.azure.com/"), # type: ignore
        organization=os.getenv("ADO_ORGANIZATION", "elobr"),
        team=os.getenv("ADO_TEAM", "Elo"),
        project=os.getenv("ADO_PROJECT", "Estrategia%20e%20Transformacao"),
        personal_access_token=req.pat if req.pat else os.getenv("ADO_PAT", ""),
        top = int(req.top or os.getenv("ADO_TOP", 50))
    )

    allowed_fields = req.parameters.allowed_fields or allowed_fields_default
    params = WIQLQueryParams(
        days_back=int(os.getenv("ADO_DAYS_BACK", 60)),
        excluded_states=os.getenv("ADO_EXCLUDED_STATES", "Completed,Canceled,Done,Resolved,Closed").split(","),
        area_paths=req.parameters.area_paths,
        value_filters=req.parameters.value_filters,
        keyword_filters=req.parameters.keyword_filters,
        query=req.parameters.query,
        allowed_fields=allowed_fields
    )
    sorted_fields = sorted(allowed_fields.keys())
    work_items = get_work_items(params, config)
    logger.info(f"Found {len(work_items)} work items.")
    return Response(header=[allowed_fields[h]['title'] for h in sorted_fields],
                    values=work_items)
    
app = func.AsgiFunctionApp(app=fapi_app, http_auth_level=func.AuthLevel.ANONYMOUS)
