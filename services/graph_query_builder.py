"""
Graph Query Builder Service
===========================

Service that reads YAML flow configs and executes Cypher queries on Neo4j.

**READ-ONLY DATA ACCESS - NO CREATE/UPDATE/DELETE**

Architecture
------------
GraphQueryBuilder performs:

1. **Load Flow Config** - Read traversal-step definitions from YAML
2. **Build Cypher Query** - Build queries from templates and entity info
3. **Execute Query** - Run read-only queries on Neo4j
4. **Process Results** - Filter and format output data

Flow
----
::

    Flow YAML File
         │
         ▼
    ┌──────────────────┐
    │ Load Config      │ ← Parse YAML file
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ For each step:   │
    │ ├─ Build Query   │ ← Replace placeholders
    │ ├─ Validate RO   │ ← Ensure READ-ONLY
    │ ├─ Execute       │ ← Run on Neo4j
    │ └─ Process       │ ← Filter attributes
    └────────┬─────────┘
             │
             ▼
    QueryFlowResult

Security
--------
All queries are validated to ensure read-only behavior:

- Forbidden keywords: CREATE, MERGE, DELETE, REMOVE, SET, DROP, DETACH
- Uses read-only transaction patterns in Neo4j

Flow YAML Structure
-------------------
::

    flow_name: "why_university"
    description: "Query flow for university questions"

    supported_entity_types:
      - University

    requires_entity_id: false  # true if entity_id is required

    traversal_steps:
      - step: 1
        name: "university_info"
        description: "Get university information"
        cypher_template: >
          MATCH (u:University)
          RETURN u
        attributes:
          - name
          - description

Environment Variables
---------------------
- NEO4J_URI: Neo4j connection URI (bolt://...)
- NEO4J_USER: Username
- NEO4J_PASSWORD: Password

Usage
-----
::

    from services.graph_query_builder import get_graph_query_builder

    builder = get_graph_query_builder()

    result = await builder.execute_flow(
        flow_file="why_university.yaml",
        resolved_entity=entity
    )

    print(result.traversal_results["university_info"].data)
"""

import re

import yaml
from neo4j import AsyncGraphDatabase, GraphDatabase

from configs.config_service import get_settings
from dbs.graph_search_helpers import sanitize_neo4j_types
from schemas.query_plan_config import QueryFlowResult, ResolvedEntity, TraversalStepResult
from tools.QA.services.neo4j_service import build_temporal_where_clause
from utils.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)
# logger.addHandler(logging.StreamHandler(sys.stdout))

# =============================================================================
# GRAPH QUERY BUILDER
# =============================================================================


class GraphQueryBuilder:
    """
    Service to build and execute Cypher queries from YAML flow configs.

    **READ-ONLY OPERATIONS ONLY**

    Attributes
    ----------
    FORBIDDEN_KEYWORDS : list[str]
            Keywords not allowed in queries (to enforce read-only behavior).
    uri : str
            Neo4j connection URI.
    config_base_path : Path
            Base path cho flow config files.

    Example
    -------
    >>> builder = GraphQueryBuilder()
    >>> config = builder.load_flow_config("why_university.yaml")
    >>> print(config["flow_name"])
    'why_university'
    """

    FORBIDDEN_KEYWORDS = [
        "CREATE",
        "MERGE",
        "DELETE",
        "REMOVE",
        "SET",
        "DROP",
        "DETACH",
        "CALL.*apoc.*create",
        "CALL.*apoc.*merge",
    ]

    def __init__(self):
        """Initialize GraphQueryBuilder from environment variables."""
        self.uri = settings.neo4j_uri
        self.user = settings.neo4j_user
        self.password = settings.neo4j_password
        self._driver = None
        self._async_driver = None
        self.config_base_path = settings.query_plan_dir

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    def _get_driver(self):
        """
        Lazily initialize the synchronous Neo4j driver.

        Returns
        -------
        neo4j.Driver
                Sync Neo4j driver instance.
        """
        if self._driver is None:
            self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._driver

    def _get_async_driver(self):
        """
        Lazily initialize the asynchronous Neo4j driver.

        Returns
        -------
        neo4j.AsyncDriver
                Async Neo4j driver instance.
        """
        if self._async_driver is None:
            self._async_driver = AsyncGraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._async_driver

    # =========================================================================
    # SECURITY VALIDATION
    # =========================================================================

    def _validate_query_readonly(self, query: str) -> bool:
        """
        Validate that the query is read-only.

        Parameters
        ----------
        query : str
                Cypher query string.

        Returns
        -------
        bool
                True when the query is safe.

        Raises
        ------
        ValueError
                If the query contains forbidden keywords.
        """
        query_upper = query.upper()
        for keyword in self.FORBIDDEN_KEYWORDS:
            if keyword in query_upper:
                raise ValueError(f"Query contains forbidden keyword '{keyword}'. Only READ operations are allowed.")
        return True

    # =========================================================================
    # FLOW CONFIG LOADING
    # =========================================================================

    def load_flow_config(self, flow_file: str) -> dict:
        """
        Load a YAML flow config from file.

        Parameters
        ----------
        flow_file : str
                YAML filename (for example: "why_university.yaml").

        Returns
        -------
        dict
                Parsed flow config.

        Raises
        ------
        FileNotFoundError
                If the file does not exist.
        """
        flow_path = self.config_base_path / flow_file
        if not flow_path.exists():
            raise FileNotFoundError(f"Flow config does not exist: {flow_path}")

        with open(flow_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    # =========================================================================
    # QUERY BUILDING
    # =========================================================================

    def build_cypher_query(self, cypher_template: str, entity_type: str, entity_id: str) -> str:
        """
        Build a Cypher query from a template.

        Parameters
        ----------
        cypher_template : str
                Query template with placeholders ({entity_type}, etc.).
        entity_type : str
                Entity type (Major, Faculty, etc.).
        entity_id : str
                Entity identifier.

        Returns
        -------
        str
                Formatted Cypher query string.

        Raises
        ------
        ValueError
                If the query is not read-only.
        """
        query = cypher_template.replace("{entity_type}", entity_type)
        self._validate_query_readonly(query)
        return query

    def _inject_temporal_filters(
        self, query: str, params: dict
    ) -> tuple[str, dict]:
        """
        Replace ``{temporal_filter:ALIAS}`` placeholders in a Cypher query
        with the clause generated by ``build_temporal_where_clause``.

        Temporal parameters (``effective_from``, ``effective_to``) are consumed
        from *params* and baked into the query string so they no longer need
        to be passed as Neo4j parameters.

        If no placeholders are found the query and params are returned
        unchanged.
        """
        pattern = re.compile(r"\{temporal_filter:(\w+)\}")
        matches = pattern.findall(query)
        if not matches:
            return query, params

        effective_from = params.get("effective_from")
        effective_to = params.get("effective_to")

        for alias in matches:
            clause = build_temporal_where_clause(alias, effective_from, effective_to)
            replacement = clause if clause else "true"
            query = query.replace(f"{{temporal_filter:{alias}}}", replacement)

        # Remove temporal keys from params — they are now baked into the query
        cleaned = {k: v for k, v in params.items() if k not in ("effective_from", "effective_to")}
        return query, cleaned

    # =========================================================================
    # QUERY EXECUTION
    # =========================================================================

    def execute_query_sync(self, query: str, parameters: dict) -> list[dict]:
        """
        Execute a Cypher query synchronously (read-only).

        Parameters
        ----------
        query : str
                Cypher query string.
        parameters : dict
                Query parameters.

        Returns
        -------
        list[dict]
                List of record dictionaries.
        """
        self._validate_query_readonly(query)

        driver = self._get_driver()
        with driver.session() as session:
            result = session.run(query, parameters)
            return result.data()

    # =========================================================================
    # FLOW EXECUTION
    # =========================================================================

    def execute_flow(
        self, flow_file: str, resolved_entity: ResolvedEntity, extra_params: dict | None = None
    ) -> QueryFlowResult:
        """
        Execute a full query flow for a single entity.

        Main entry point for flow execution. Runs all traversal
        steps defined in the YAML flow file.

        Parameters
        ----------
        flow_file : str
                YAML flow config filename.
        resolved_entity : ResolvedEntity
                Entity already resolved from Milvus.

        Returns
        -------
        QueryFlowResult
                Result containing data from all traversal steps.
        """
        # Load config
        flow_config = self.load_flow_config(flow_file)

        entity_type = resolved_entity.entity_type
        entity_id = resolved_entity.entity_id
        requires_entity_id = flow_config.get("requires_entity_id", True)

        traversal_results: dict[str, TraversalStepResult] = {}

        for step_config in flow_config.get("traversal_steps", []):
            step_result = self._execute_step(
                step_config=step_config,
                entity_type=entity_type,
                entity_id=entity_id,
                requires_entity_id=requires_entity_id,
                extra_params=extra_params,
            )
            if step_result:
                traversal_results[step_result.step_name] = step_result

        # Build context for LLM
        raw_context = self._build_context(flow_config, resolved_entity, traversal_results)

        return QueryFlowResult(
            resolved_entity=resolved_entity, traversal_results=traversal_results, raw_context=raw_context
        )

    def _execute_step(
        self,
        step_config: dict,
        entity_type: str,
        entity_id: str,
        requires_entity_id: bool,
        extra_params: dict | None = None,
    ) -> TraversalStepResult | None:
        """
        Execute a single traversal step.

        Parameters
        ----------
        step_config : dict
                Step config loaded from YAML.
        entity_type : str
                Entity type.
        entity_id : str
                Entity identifier.
        requires_entity_id : bool
                True if the flow requires entity_id.

        Returns
        -------
        TraversalStepResult, optional
                Step result, or None when the step is skipped.
        """
        step_name = step_config["name"]

        # Check if step only applies to specific entity types
        only_for_types = step_config.get("only_for_types", [])
        if only_for_types and entity_type not in only_for_types:
            return None

        # Build query
        cypher_template = step_config["cypher_template"]
        query = self.build_cypher_query(cypher_template, entity_type, entity_id or "")

        # Build parameters
        params = {}
        if requires_entity_id and entity_id:
            params["entity_id"] = entity_id
        if extra_params:
            params.update(extra_params)

        query, params = self._inject_temporal_filters(query, params)

        try:
            raw_results = self.execute_query_sync(query, params)

            # Get attributes to extract
            attributes = self._get_step_attributes(step_config, entity_type)

            # Process results
            processed_data = self._process_step_results(raw_results, attributes)

            return TraversalStepResult(step_name=step_name, data=processed_data, cypher_query=query)

        except Exception as e:
            logger.exception("[GraphQueryBuilder] Error executing step '%s': %s", step_name, e)
            return TraversalStepResult(step_name=step_name, data=[], cypher_query=query)

    def _get_step_attributes(self, step_config: dict, entity_type: str) -> list[str]:
        """
        Get the list of attributes to extract for a step.

        Parameters
        ----------
        step_config : dict
                Step config.
        entity_type : str
                Entity type (used to check `attributes_by_type`).

        Returns
        -------
        list[str]
                List of attribute names.
        """
        attributes = step_config.get("attributes", [])
        attributes_by_type = step_config.get("attributes_by_type", {})

        if attributes_by_type and entity_type in attributes_by_type:
            attributes = attributes_by_type[entity_type]

        return attributes

    def _process_step_results(self, raw_results: list[dict], attributes: list[str]) -> list[dict]:
        """
        Process and filter results returned by a Neo4j query.

        Parameters
        ----------
        raw_results : list[dict]
                Raw Neo4j results.
        attributes : list[str]
                Attributes to keep (empty means keep all attributes).

        Returns
        -------
        list[dict]
                Processed and filtered data.
        """
        processed_data = []

        for record in sanitize_neo4j_types(raw_results):
            if not record:
                continue

            record_data = {}

            # Extract node data (dict values)
            for _key, value in record.items():
                if isinstance(value, dict):
                    record_data.update(value)
                    break

            # Add scalar values (relationship attrs aliased in RETURN)
            for key, value in record.items():
                if not isinstance(value, dict) and value is not None:
                    record_data[key] = value

            # Filter by attributes
            filtered_data = {}
            if not attributes:
                filtered_data = {k: v for k, v in record_data.items() if v is not None}
            else:
                for k, v in record_data.items():
                    if v is None:
                        continue

                    if k in attributes:
                        filtered_data[k] = v
                    elif "." in k:
                        # Handle prefixed keys (e.g., p.name -> name)
                        suffix = k.split(".")[-1]
                        if suffix in attributes:
                            filtered_data[suffix] = v

            if filtered_data:
                processed_data.append(filtered_data)

        return processed_data

    # =========================================================================
    # CONTEXT BUILDING
    # =========================================================================

    def _build_context(
        self, flow_config: dict, resolved_entity: ResolvedEntity, traversal_results: dict[str, TraversalStepResult]
    ) -> str:
        """
        Build a formatted context string for the LLM.

        Parameters
        ----------
        flow_config : dict
                Flow configuration.
        resolved_entity : ResolvedEntity
                Resolved entity info.
        traversal_results : dict
                Results from all traversal steps.

        Returns
        -------
        str
                Formatted markdown context string.
        """
        entity_type_mapping = flow_config.get("entity_type_mapping", {})
        entity_type_vi = entity_type_mapping.get(resolved_entity.entity_type, resolved_entity.entity_type)

        context_parts = []

        self._add_main_entity_context(context_parts, traversal_results, entity_type_vi, resolved_entity)
        self._add_specializations_context(context_parts, traversal_results)
        self._add_programs_context(context_parts, traversal_results)
        self._add_outcomes_context(context_parts, traversal_results)
        self._add_market_trends_context(context_parts, traversal_results)
        self._add_faculty_context(context_parts, traversal_results)
        self._add_partners_context(context_parts, traversal_results)

        return "\n".join(context_parts)

    def _add_main_entity_context(self, parts: list, results: dict, entity_type_vi: str, entity: ResolvedEntity):
        """Add main entity context."""
        if "main_entity" in results:
            main_data = results["main_entity"].data
            if main_data:
                parts.append(f"## Thông tin {entity_type_vi}: {entity.entity_name}")
                for item in main_data:
                    for key, value in item.items():
                        if value:
                            parts.append(f"- {key}: {value}")

    def _add_specializations_context(self, parts: list, results: dict):
        """Add specializations context."""
        if "specializations" in results:
            specs = results["specializations"].data
            if specs:
                parts.append("\n## Các chuyên ngành")
                for spec in specs:
                    parts.append(f"\n### {spec.get('name', 'N/A')}")
                    for key, value in spec.items():
                        if value and key != "name":
                            parts.append(f"- {key}: {value}")

    def _add_programs_context(self, parts: list, results: dict):
        """Add academic programs context."""
        if "academic_programs" in results:
            programs = results["academic_programs"].data
            if programs:
                parts.append("\n## Chương trình đào tạo")
                for prog in programs:
                    parts.append(f"\n### {prog.get('name', 'N/A')}")
                    for key, value in prog.items():
                        if value and key != "name":
                            parts.append(f"- {key}: {value}")

    def _add_outcomes_context(self, parts: list, results: dict):
        """Add learning outcomes context."""
        if "learning_outcomes" in results:
            outcomes = results["learning_outcomes"].data
            if outcomes:
                parts.append("\n## Chuẩn đầu ra")
                for i, outcome in enumerate(outcomes[:10], 1):
                    text = outcome.get("description") or outcome.get("name", "")
                    if text:
                        parts.append(f"{i}. {text}")

    def _add_market_trends_context(self, parts: list, results: dict):
        """Add market trends context."""
        if "market_trends" in results:
            trends = results["market_trends"].data
            if trends:
                parts.append("\n## Xu hướng thị trường")
                for trend in trends:
                    parts.append(f"\n### {trend.get('name', 'N/A')}")
                    if trend.get("description"):
                        parts.append(f"- Mô tả: {trend['description']}")
                    if trend.get("demand_size"):
                        parts.append(f"- Nhu cầu: {trend['demand_size']}")
                    if trend.get("growth_rate"):
                        parts.append(f"- Tốc độ tăng trưởng: {trend['growth_rate']}%")

    def _add_faculty_context(self, parts: list, results: dict):
        """Add faculty context."""
        if "faculty_info" in results:
            faculty = results["faculty_info"].data
            if faculty:
                parts.append("\n## Khoa đào tạo")
                for f in faculty:
                    parts.append(f"- Tên: {f.get('name', 'N/A')}")
                    if f.get("description"):
                        parts.append(f"- Mô tả: {f['description']}")

    def _add_partners_context(self, parts: list, results: dict):
        """Add partners context."""
        if "partners" in results:
            partners = results["partners"].data
            if partners:
                parts.append("\n## Đối tác hợp tác")
                for p in partners:
                    parts.append(f"- {p.get('name', 'N/A')}")

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def close(self):
        """Close open Neo4j connections."""
        if self._driver:
            self._driver.close()
            self._driver = None


# =============================================================================
# SINGLETON
# =============================================================================

_graph_query_builder: GraphQueryBuilder | None = None


def get_graph_query_builder() -> GraphQueryBuilder:
    """
    Get the singleton GraphQueryBuilder instance.

    Uses a singleton pattern to reuse the Neo4j connection
    and flow config cache.

    Returns
    -------
    GraphQueryBuilder
            Shared GraphQueryBuilder instance.

    Example

    Notes
    -----
    - Uses lazy initialization for the Neo4j driver.
    - Loads flow configs from `config/query_flows/`.
    - Read-only behavior: queries are validated as READ-ONLY.
    - Forbidden keywords: CREATE, MERGE, DELETE, SET, DROP, DETACH
    """
    global _graph_query_builder
    if _graph_query_builder is None:
        _graph_query_builder = GraphQueryBuilder()
    return _graph_query_builder
