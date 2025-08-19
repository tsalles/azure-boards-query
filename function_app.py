import azure.functions as func
import logging
import os
import base64
from typing import List
from pydantic import BaseModel, Field, HttpUrl
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
from bs4 import BeautifulSoup
import html
import fastapi
from dotenv import load_dotenv
load_dotenv()


class AzureDevOpsConfig(BaseModel):
    """Configuration for Azure DevOps connection."""
    query: str = Field(..., description="WIQL query string")
    base_url: HttpUrl = Field(..., description="Base WIQL API URL")
    organization: str = Field(..., description="Azure DevOps Organization name")
    team: str = Field(..., description="Azure DevOps Team name")
    project: str = Field(..., description="Azure DevOps Project name")
    personal_access_token: str = Field(..., description="Azure DevOps Personal Access Token")
    top: int = Field(default=50, description="Max number of work items to return")


class WIQLQueryParams(BaseModel):
    """Parameters for the WIQL query."""
    days_back: int = Field(default=60, description="Number of days back from today")
    excluded_states: List[str] = Field(default_factory=lambda: ["Completed", "Canceled", "Done", "Closed", "Resolved"],)
    area_paths: List[str] = Field(default_factory=list, description="List of area paths to filter by. If empty, no area path filter is applied.")


class WIQLRequestBody(BaseModel):
    """Request body for the WIQL query endpoint."""
    pat: str = Field(..., description="Personal Access Token for Azure DevOps")
    query: str = Field(..., description="WIQL query string")
    top: int = Field(default=50, description="Max number of work items to return")
    timeprecision: bool = Field(default=True, description="Whether to include time precision in the query")
    parameters: WIQLQueryParams = Field(default_factory=lambda: WIQLQueryParams())


class Response(BaseModel):
    """Response model for the WIQL query endpoint."""
    work_items: List[str] = Field(..., description="List of work items returned by the WIQL query") 


def get_ado_client(config: AzureDevOpsConfig):
    credentials = BasicAuthentication('', config.personal_access_token)
    connection = Connection(base_url=f'{config.base_url}/{config.organization}',
                            creds=credentials)
    return connection.clients.get_work_item_tracking_client()


def get_work_items(wiql_query: str, config: AzureDevOpsConfig) -> List[str]:
    """
    Executes the WIQL query and retrieves work items from Azure DevOps Boards.
    """
    from azure.devops.v7_1.work_item_tracking import WorkItemTrackingClient
    client = get_ado_client(config)
    wiql = {"query": wiql_query}
    result = client.query_by_wiql(wiql)
    ids = [item.id for item in result.work_items]
    if not ids:
        return []
   
    work_items = []
    batch_size = 199
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i:i+batch_size]
        batch_items = client.get_work_items(batch_ids, expand='all')
        for work_item in batch_items:
            wi = build_work_item(work_item)
            if wi: 
                work_items.append(wi)

    return work_items 


def build_query(params: WIQLQueryParams, config: AzureDevOpsConfig) -> str:
    """
    Builds the WIQL query string with given parameters.
    """
    team = config.team
    excluded_states_str = ", ".join(f"'{state}'" for state in params.excluded_states)

    area_path_filter = ""
    if params.area_paths:
        if len(params.area_paths) == 1:
            area_path = params.area_paths[0].replace("'", "\\'")
            area_path_filter = f"AND ([System.AreaPath] UNDER '{area_path}') "
        else:
            or_conditions = " OR ".join(
                "[System.AreaPath] UNDER '"+ap.replace("'", "\\'")+"'" for ap in params.area_paths
            )
            area_path_filter = f"AND ( {or_conditions} ) "

    return (
        f"SELECT [System.Id], [System.WorkItemType], [System.Title], "
        f"[System.State], [System.AreaPath], [System.IterationPath] "
        f"FROM WorkItems "
        f"WHERE [System.TeamProject] = '{team}' "
        f"AND [System.CreatedDate] >= @Today - {params.days_back} "
        f"AND [System.State] NOT IN ({excluded_states_str}) "
        f"{area_path_filter}"
        f"ORDER BY [System.ChangedDate] DESC"
    )


def get_auth_header(token: str) -> dict[str, str]:
    """
    Returns the HTTP Authorization header for Azure DevOps using Basic Auth.
    Username is empty, password is the PAT.
    """
    credentials = f":{token}".encode("utf-8")
    encoded_credentials = base64.b64encode(credentials).decode("utf-8")
    return {"Authorization": f"Basic {encoded_credentials}"}


def run_wiql_query(config: AzureDevOpsConfig, params: WIQLQueryParams) -> List[str]:
    """
    Executes the WIQL query against Azure DevOps Boards.
    """
    query_str = config.query if config.query else build_query(params, config)
    return get_work_items(query_str, config)


def build_url(base_url: str, organization: str, project: str, team: str, top: int) -> str:
    """
    Constructs the full URL for the Azure DevOps API.
    Template: https://dev.azure.com/{organization}/{project}/{team}/_apis/wit/wiql?timePrecision={timePrecision}&$top={$top}&api-version=7.1
    """
    return f"{base_url}{organization}/{team}/{project}/_apis/wit/wiql?timePrecision=true&top={top}"


def build_work_item(work_item) -> str:
    """
    Builds a string representation of the work item.
    """
    allowed_fields = [
        "System.Id",
        "System.Title",
        "System.WorkItemType",
        "System.State",
        "System.AreaPath",
        "System.AssignedTo",
        "System.Tags",
        "System.Description",
        "System.Parent",
        "Custom.GanhoQuantitativo",
        "Custom.Metadiretoriaassociada",
        "Custom.MetaEloassociada",
        "Custom.Classification",
        "Custom.TipodeIniciativa",
        "Custom.Vertical",
        "Custom.Temdependencia",
        "Custom.Acatado",
        "Custom.GrauWSprints",
        "Custom.Stakeholders"
    ]
    if "System.Description" in work_item.fields:
        lines = []
        for f in allowed_fields:
            if f == "System.Description" and f in work_item.fields and work_item.fields[f]:
                try:
                    decoded_str = work_item.fields[f].encode().decode("unicode_escape")
                except:
                    decoded_str = work_item.fields[f]
                soup = BeautifulSoup(decoded_str, "html.parser")
                description = html.unescape(soup.get_text(separator=" "))
                lines.append(f"System.Description: {description}")
            elif f in work_item.fields:
                lines.append(f"{f}: {work_item.fields[f]}")
        return "\n".join(lines)
    

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
    response_model=Response,
    tags=["Azure Boards WIQL"],
    summary="Executa uma consulta WIQL",
    description="Executa uma consulta WIQL no Azure DevOps Boards e retorna os itens de trabalho correspondentes."
)
def azure_board_query(req: WIQLRequestBody) -> Response:
    logging.info('Python HTTP trigger function processed a request.')
    config = AzureDevOpsConfig(
        query=req.query,
        base_url=os.getenv("ADO_BASE_URL", "https://dev.azure.com/"), # type: ignore
        organization=os.getenv("ADO_ORGANIZATION", "elobr"),
        team=os.getenv("ADO_TEAM", "Elo"),
        project=os.getenv("ADO_PROJECT", "Estrategia%20e%20Transformacao"),
        personal_access_token=req.pat if req.pat else os.getenv("ADO_PAT", ""),
        top=int(req.top if req.top else os.getenv("ADO_TOP", req.top))
    )

    params = WIQLQueryParams(
        days_back=int(os.getenv("ADO_DAYS_BACK", 60)),
        excluded_states=os.getenv("ADO_EXCLUDED_STATES", "Completed,Canceled,Done,Resolved,Closed").split(",")
    )

    work_items = run_wiql_query(config, params)
    if not work_items:
        return Response(work_items=[])
    logging.info(f"Found {len(work_items)} work items.")
    logging.info(f"Types: {type(work_items)} of {type(work_items[0]) if work_items else 'None'}")
    return Response(work_items=work_items)
    
app = func.AsgiFunctionApp(app=fapi_app, http_auth_level=func.AuthLevel.ANONYMOUS)
