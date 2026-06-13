import importlib
import json
import sys
import types
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch


def _install_test_stubs():
    google_module = types.ModuleType("google")
    adk_module = types.ModuleType("google.adk")
    adk_agents_module = types.ModuleType("google.adk.agents")
    adk_callback_context_module = types.ModuleType("google.adk.agents.callback_context")
    adk_tools_module = types.ModuleType("google.adk.tools")
    adk_tool_context_module = types.ModuleType("google.adk.tools.tool_context")
    adk_callback_context_module.CallbackContext = object
    adk_tools_module.ToolContext = object
    adk_tool_context_module.ToolContext = object
    sys.modules.setdefault("google", google_module)
    sys.modules.setdefault("google.adk", adk_module)
    sys.modules.setdefault("google.adk.agents", adk_agents_module)
    sys.modules.setdefault("google.adk.agents.callback_context", adk_callback_context_module)
    sys.modules.setdefault("google.adk.tools", adk_tools_module)
    sys.modules.setdefault("google.adk.tools.tool_context", adk_tool_context_module)

    pymilvus_module = types.ModuleType("pymilvus")

    class _Collection:
        def __init__(self, *args, **kwargs):
            pass

        def load(self):
            return None

        def search(self, *args, **kwargs):
            return []

    pymilvus_module.Collection = _Collection
    sys.modules.setdefault("pymilvus", pymilvus_module)

    milvus_helper_module = types.ModuleType("dbs.milvus_helper")
    milvus_helper_module.connect_milvus = lambda: True
    milvus_helper_module.get_embedding = lambda keys: keys
    milvus_helper_module.get_node_name_by_id = lambda node_id: f"node:{node_id}"
    milvus_helper_module.insert_crawled_data = lambda **kwargs: True
    milvus_helper_module.search_crawled_data = lambda **kwargs: []
    milvus_helper_module.search_common_qa = lambda **kwargs: []
    sys.modules.setdefault("dbs.milvus_helper", milvus_helper_module)

    neo4j_service_module = types.ModuleType("tools.QA.services.neo4j_service")

    class _Neo4jService:
        def cypher_query(self, query, params):
            return {"status": "success", "results": []}

    def _check_temporal_overlap(entity_from, entity_to, query_from, query_to):
        ef = entity_from or ""
        et = entity_to or ""
        qf = query_from or ""
        qt = query_to or ""

        if not ef and not et:
            return True

        def _le(a, b):
            n = min(len(a), len(b))
            return a[:n] <= b[:n]

        def _ge(a, b):
            n = min(len(a), len(b))
            return a[:n] >= b[:n]

        if qf and qt:
            if ef and et:
                return _le(ef, qt) and _ge(et, qf)
            if ef:
                return _le(ef, qt)
            return _ge(et, qf)

        if qf:
            return _ge(et, qf) if et else True
        if qt:
            return _le(ef, qt) if ef else True
        return True

    neo4j_service_module.Neo4jService = _Neo4jService
    neo4j_service_module.check_temporal_overlap = _check_temporal_overlap
    sys.modules.setdefault("tools.QA.services.neo4j_service", neo4j_service_module)

    graph_helpers_module = types.ModuleType("dbs.graph_search_helpers")
    graph_helpers_module.sanitize_neo4j_types = lambda data: data
    graph_helpers_module._normalize_temporal_params = lambda time: (None, None)
    sys.modules.setdefault("dbs.graph_search_helpers", graph_helpers_module)

    keyword_helper_module = types.ModuleType("dbs.keyword_search_milvus_helper")
    keyword_helper_module.util = SimpleNamespace(get_best=lambda question: None)
    sys.modules.setdefault("dbs.keyword_search_milvus_helper", keyword_helper_module)

    schema_module = types.ModuleType("schemas.query_plan_config")

    class _FlowConfig:
        def __init__(self, flow_file="", entities_to_resolve=None, subtopics=None, time_filter=None):
            self.flow_file = flow_file
            self.entities_to_resolve = entities_to_resolve or []
            self.subtopics = subtopics or []
            self.time_filter = time_filter

    class _IntentClassification:
        def __init__(self, flow_config):
            self.flow_config = flow_config

    schema_module.FlowConfig = _FlowConfig
    schema_module.IntentClassification = _IntentClassification
    schema_module.MultiEntityQueryResult = object
    schema_module.QueryFlowResult = object
    schema_module.ResolvedEntity = object
    sys.modules.setdefault("schemas.query_plan_config", schema_module)

    entity_resolver_module = types.ModuleType("services.entity_resolver")
    entity_resolver_module.EntityResolver = object
    entity_resolver_module.get_entity_resolver = lambda: object()
    sys.modules.setdefault("services.entity_resolver", entity_resolver_module)

    function_flow_executor_module = types.ModuleType("services.function_flow_executor")

    class _FunctionFlowExecutor:
        def execute_flow(self, flow_file_path, input_data):
            return {}

    function_flow_executor_module.FunctionFlowExecutor = _FunctionFlowExecutor
    sys.modules.setdefault("services.function_flow_executor", function_flow_executor_module)

    graph_query_builder_module = types.ModuleType("services.graph_query_builder")
    graph_query_builder_module.GraphQueryBuilder = object
    graph_query_builder_module.get_graph_query_builder = lambda: SimpleNamespace(config_base_path="/tmp/config")
    sys.modules.setdefault("services.graph_query_builder", graph_query_builder_module)

    parallel_web_search_module = types.ModuleType("utils.parallel_web_search")
    parallel_web_search_module.run_parallel_web_search_sync = lambda **kwargs: {}
    sys.modules.setdefault("utils.parallel_web_search", parallel_web_search_module)

    config_service_module = types.ModuleType("configs.config_service")
    config_service_module.get_settings = lambda: SimpleNamespace(
        kogito_endpoint_addressing_rule="http://localhost:8080",
        answer_plan_dir="/tmp",
        mcp_host="localhost",
        mcp_port=3001,
    )
    config_service_module.get_config_service = lambda: SimpleNamespace()
    sys.modules.setdefault("configs.config_service", config_service_module)

    requests_module = types.ModuleType("requests")

    class _RequestException(Exception):
        pass

    requests_module.RequestException = _RequestException
    requests_module.post = lambda *args, **kwargs: None
    sys.modules.setdefault("requests", requests_module)

    langchain_core_module = types.ModuleType("langchain_core")
    langchain_tracers_module = types.ModuleType("langchain_core.tracers")
    langchain_tracer_impl_module = types.ModuleType("langchain_core.tracers.langchain")
    langchain_tracer_impl_module.log_error_once = lambda *args, **kwargs: None
    sys.modules.setdefault("langchain_core", langchain_core_module)
    sys.modules.setdefault("langchain_core.tracers", langchain_tracers_module)
    sys.modules.setdefault("langchain_core.tracers.langchain", langchain_tracer_impl_module)

    query_agents_module = types.ModuleType("schemas.query_agents")
    query_agents_module.QueryAgentOutputSchemaV2 = object
    sys.modules.setdefault("schemas.query_agents", query_agents_module)

    confident_query_module = types.ModuleType("tools.confident_query_hint_processing")
    confident_query_module.ConfidentQueryHintProcessor = object
    sys.modules.setdefault("tools.confident_query_hint_processing", confident_query_module)

    confidence_score_module = types.ModuleType("utils.calculate_confidence_score_query_results")
    confidence_score_module.calculate_confidence_score = lambda *args, **kwargs: {}
    sys.modules.setdefault("utils.calculate_confidence_score_query_results", confidence_score_module)

    httpx_module = types.ModuleType("httpx")

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

    httpx_module.AsyncClient = _AsyncClient
    sys.modules.setdefault("httpx", httpx_module)


_install_test_stubs()

what_query_results_module = importlib.import_module("schemas.what_query_results")
WhatAnswer = what_query_results_module.WhatAnswer
WhatAnswerKind = what_query_results_module.WhatAnswerKind
WhatIntent = what_query_results_module.WhatIntent
WhatMeta = what_query_results_module.WhatMeta
WhatQueryResult = what_query_results_module.WhatQueryResult
WhatSource = what_query_results_module.WhatSource

query_plan_execute_module = importlib.import_module("services.query_plan_execute")
QueryOrchestrator = query_plan_execute_module.QueryOrchestrator

reasoning_processing = importlib.import_module("tools.reasoning_processing")
query_internal_data = importlib.import_module("tools.query_agent").query_internal_data

response_prompt_builder_module = importlib.import_module("tools.response_agent_prompt_builder")
AnswerQueryPromptState = response_prompt_builder_module.AnswerQueryPromptState
CounselorPlaybookPromptState = response_prompt_builder_module.CounselorPlaybookPromptState
ResponsePolishingPromptState = response_prompt_builder_module.ResponsePolishingPromptState
_compact_prompt_block_content = response_prompt_builder_module._compact_prompt_block_content
_is_answer_empty = response_prompt_builder_module._is_answer_empty
build_answer_query_prompt = response_prompt_builder_module.build_answer_query_prompt
build_counselor_playbook_prompt = response_prompt_builder_module.build_counselor_playbook_prompt
build_response_polishing_prompt = response_prompt_builder_module.build_response_polishing_prompt

prepare_data_module = importlib.import_module("utils.prepare_data_for_dmn_reasoning")
prepare_reasoning_input = prepare_data_module.prepare_reasoning_input
prepare_relation_enrich_input = prepare_data_module.prepare_relation_enrich_input


class DummyToolContext:
    def __init__(self):
        self.state = {}


class DummyState(dict):
    def to_dict(self):
        return dict(self)


class ProcessReasoningTests(unittest.TestCase):
    def test_normalize_reasoning_input_map_limits_number_of_nodes(self):
        input_data = {f"node-{index}": ["code"] for index in range(reasoning_processing.ENRICH_MAX_INPUT_ITEMS + 3)}

        result = reasoning_processing._normalize_reasoning_input_map(input_data)

        self.assertEqual(len(result), reasoning_processing.ENRICH_MAX_INPUT_ITEMS)
        self.assertEqual(list(result.keys())[0], "node-0")
        self.assertEqual(
            list(result.keys())[-1],
            f"node-{reasoning_processing.ENRICH_MAX_INPUT_ITEMS - 1}",
        )

    def test_normalize_reasoning_input_treats_single_string_attr_as_one_key(self):
        item = reasoning_processing._normalize_reasoning_input_item(
            {
                "req_attrs": "fee_information",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": "2026-12-31T23:59:59Z",
            }
        )

        self.assertEqual(item["req_attrs"], ["fee_information"])

    def test_find_siblings_filters_out_non_overlapping_temporal_siblings(self):
        input_data = {
            "node-1": {
                "req_attrs": ["fee_information"],
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": "2026-12-31T23:59:59Z",
            }
        }
        records = [
            {
                "source_node_id": "node-1",
                "props": {"name": "Policy 2025", "fee_information": "2025"},
                "sibling_effective_from": "2025-01-01T00:00:00Z",
                "sibling_effective_to": "2025-12-31T23:59:59Z",
            },
            {
                "source_node_id": "node-1",
                "props": {"name": "Policy 2026", "fee_information": "2026"},
                "sibling_effective_from": "2026-01-01T00:00:00Z",
                "sibling_effective_to": "2026-12-31T23:59:59Z",
            },
        ]

        with patch.object(reasoning_processing.Neo4jUtils, "run_query", return_value=records):
            result = reasoning_processing.find_siblings_with_attributes(input_data)

        self.assertEqual(result, [{"name": "Policy 2026", "fee_information": "2026"}])

    def test_find_siblings_accepts_datetime_temporal_values(self):
        input_data = {
            "node-1": {
                "req_attrs": ["fee_information"],
                "effective_from": "2026-01-01T00:00:00+00:00",
                "effective_to": "2026-12-31T23:59:59+00:00",
            }
        }
        records = [
            {
                "source_node_id": "node-1",
                "props": {"name": "Policy 2025", "fee_information": "2025"},
                "sibling_effective_from": datetime(2025, 1, 1, tzinfo=UTC),
                "sibling_effective_to": datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
            },
            {
                "source_node_id": "node-1",
                "props": {"name": "Policy 2026", "fee_information": "2026"},
                "sibling_effective_from": datetime(2026, 1, 1, tzinfo=UTC),
                "sibling_effective_to": datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
            },
        ]

        with patch.object(reasoning_processing.Neo4jUtils, "run_query", return_value=records):
            result = reasoning_processing.find_siblings_with_attributes(input_data)

        self.assertEqual(result, [{"name": "Policy 2026", "fee_information": "2026"}])

    def test_process_reasoning_preserves_temporal_bounds_for_enriched_sibling_search(self):
        tool_context = DummyToolContext()
        input_data = {
            "node-1": {
                "req_attrs": ["fee_information"],
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": "2026-12-31T23:59:59Z",
            }
        }
        neo4j_batch = [{"id": "node-1", "candidates": ["tuition_fee"]}]
        node_candidate_scores = {"node-1": {"tuition_fee": 0.9}}

        with (
            patch.object(
                reasoning_processing, "_recommend_attributes", return_value=(neo4j_batch, node_candidate_scores)
            ),
            patch.object(reasoning_processing, "_validate_and_rank_attributes", return_value={}),
            patch.object(reasoning_processing, "find_siblings_with_attributes", side_effect=[[], []]) as sibling_search,
        ):
            reasoning_processing.process_reasoning(tool_context, input_data)

        self.assertEqual(
            sibling_search.call_args_list[0].args[0],
            {
                "node-1": {
                    "req_attrs": ["tuition_fee"],
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_to": "2026-12-31T23:59:59Z",
                }
            },
        )

    def test_build_enriched_criteria_map_limits_candidates_by_score_and_top_k(self):
        neo4j_batch = [
            {
                "id": "node-1",
                "candidates": ["description", "quota", "duration"],
            }
        ]
        input_data = {
            "node-1": {
                "req_attrs": ["code"],
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": "2026-12-31T23:59:59Z",
            }
        }
        node_candidate_scores = {
            "node-1": {
                "duration": 0.71,
                "description": 0.95,
                "quota": 0.88,
            }
        }

        result = reasoning_processing.build_enriched_criteria_map(
            neo4j_batch,
            input_data,
            top_k=2,
            node_candidate_scores=node_candidate_scores,
        )

        self.assertEqual(
            result,
            {
                "node-1": {
                    "req_attrs": ["description", "quota"],
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_to": "2026-12-31T23:59:59Z",
                }
            },
        )

    def test_process_reasoning_uses_enriched_siblings_without_fallback(self):
        tool_context = DummyToolContext()
        input_data = {"node-1": ["code"]}
        neo4j_batch = [{"id": "node-1", "candidates": ["tuition_fee"]}]
        node_candidate_scores = {"node-1": {"tuition_fee": 0.9}}
        option_1_1 = {"node-1": {"tuition_fee": "10M"}}
        option_1_2 = [{"name": "Program A", "tuition_fee": "12M"}]

        with (
            patch.object(
                reasoning_processing, "_recommend_attributes", return_value=(neo4j_batch, node_candidate_scores)
            ),
            patch.object(reasoning_processing, "_validate_and_rank_attributes", return_value=option_1_1),
            patch.object(
                reasoning_processing, "find_siblings_with_attributes", return_value=option_1_2
            ) as sibling_search,
        ):
            result = reasoning_processing.process_reasoning(tool_context, input_data)

        self.assertEqual(
            result,
            {
                "option_1_1": option_1_1,
                "option_1_2": option_1_2,
                "option_2": [],
            },
        )
        sibling_search.assert_called_once_with(
            {
                "node-1": {
                    "req_attrs": ["tuition_fee"],
                    "effective_from": None,
                    "effective_to": None,
                }
            }
        )
        self.assertEqual(json.loads(tool_context.state["data_lv2_data_enriched_nodes"]), option_1_1)
        self.assertEqual(json.loads(tool_context.state["data_lv2_siblings_relative"]), option_1_2)
        self.assertEqual(json.loads(tool_context.state["data_lv2_siblings_similar"]), [])

    def test_process_reasoning_falls_back_to_original_siblings_when_enriched_siblings_empty(self):
        tool_context = DummyToolContext()
        input_data = {"node-1": ["code"]}
        neo4j_batch = [{"id": "node-1", "candidates": ["tuition_fee"]}]
        node_candidate_scores = {"node-1": {"tuition_fee": 0.9}}
        option_1_1 = {"node-1": {"tuition_fee": "10M"}}
        option_2 = [{"name": "Program B", "code": "B01"}]

        with (
            patch.object(
                reasoning_processing, "_recommend_attributes", return_value=(neo4j_batch, node_candidate_scores)
            ),
            patch.object(reasoning_processing, "_validate_and_rank_attributes", return_value=option_1_1),
            patch.object(
                reasoning_processing, "find_siblings_with_attributes", side_effect=[[], option_2]
            ) as sibling_search,
        ):
            result = reasoning_processing.process_reasoning(tool_context, input_data)

        self.assertEqual(
            result,
            {
                "option_1_1": option_1_1,
                "option_1_2": [],
                "option_2": option_2,
            },
        )
        self.assertEqual(
            sibling_search.call_args_list[0].args[0],
            {
                "node-1": {
                    "req_attrs": ["tuition_fee"],
                    "effective_from": None,
                    "effective_to": None,
                }
            },
        )
        self.assertEqual(sibling_search.call_args_list[1].args[0], input_data)
        self.assertEqual(json.loads(tool_context.state["data_lv2_siblings_relative"]), [])
        self.assertEqual(json.loads(tool_context.state["data_lv2_siblings_similar"]), option_2)

    def test_process_reasoning_uses_original_input_when_no_candidates_exist(self):
        tool_context = DummyToolContext()
        input_data = {"node-1": ["code"]}
        option_2 = [{"name": "Program C", "code": "C01"}]

        with (
            patch.object(reasoning_processing, "_recommend_attributes", return_value=([], {})),
            patch.object(reasoning_processing, "_validate_and_rank_attributes") as validate,
            patch.object(
                reasoning_processing, "find_siblings_with_attributes", return_value=option_2
            ) as sibling_search,
        ):
            result = reasoning_processing.process_reasoning(tool_context, input_data)

        validate.assert_not_called()
        sibling_search.assert_called_once_with(input_data)
        self.assertEqual(
            result,
            {
                "option_1_1": {},
                "option_1_2": [],
                "option_2": option_2,
            },
        )


class ProcessRelationReasoningTests(unittest.TestCase):
    def test_normalize_relation_enrich_items_limits_number_of_relations(self):
        items = [
            {
                "source_node_id": f"src-{index}",
                "relation_id": f"rel-{index}",
                "relation_label": "TRAINS",
                "destination_node_id": f"dst-{index}",
                "source_node_attributes": ["name"],
                "relation_attributes": ["quota"],
            }
            for index in range(reasoning_processing.ENRICH_MAX_INPUT_ITEMS + 2)
        ]

        result = reasoning_processing._normalize_relation_enrich_items(items)

        self.assertEqual(len(result), reasoning_processing.ENRICH_MAX_INPUT_ITEMS)
        self.assertEqual(result[0]["relation_id"], "rel-0")
        self.assertEqual(
            result[-1]["relation_id"],
            f"rel-{reasoning_processing.ENRICH_MAX_INPUT_ITEMS - 1}",
        )

    def test_process_relation_reasoning_passes_global_temporal_bounds_to_sibling_search(self):
        tool_context = DummyToolContext()
        input_data = {
            "items": [
                {
                    "source_node_id": "src-1",
                    "relation_id": "rel-1",
                    "relation_label": "TRAINS",
                    "destination_node_id": "dst-1",
                    "source_node_attributes": ["name"],
                    "relation_attributes": ["quota"],
                }
            ],
            "effective_from": "2026",
            "effective_to": "2026",
        }

        with (
            patch.object(reasoning_processing, "_enrich_relation_attributes", return_value={}),
            patch.object(reasoning_processing, "_find_sibling_relations", return_value=[]) as sibling_search,
            patch.object(reasoning_processing, "_find_triangle_nodes", return_value=[]),
            patch.object(reasoning_processing, "_enrich_source_node_attributes", return_value={}) as enrich_source,
        ):
            reasoning_processing.process_relation_reasoning(tool_context, input_data)

        normalized_items = reasoning_processing._normalize_relation_enrich_items(input_data["items"])
        sibling_search.assert_any_call(
            normalized_items,
            find_dest_sibling=False,
            effective_from="2026-01-01T00:00:00Z",
            effective_to="2026-12-31T23:59:59Z",
        )
        enrich_source.assert_called_once()

    def test_find_sibling_relations_filters_out_non_overlapping_relation_and_node_temporal_bounds(self):
        items = [
            {
                "source_node_id": "src-1",
                "relation_id": "rel-1",
                "relation_label": "TRAINS",
                "destination_node_id": "dst-1",
                "source_node_attributes": ["name"],
                "relation_attributes": ["quota"],
            }
        ]
        records = [
            {
                "source_relation_id": "rel-1",
                "sibling_name": "Relation 2025",
                "sibling_node_id": "sib-2025",
                "relation_properties": {"quota": 10},
                "sibling_effective_from": "2025-01-01T00:00:00Z",
                "sibling_effective_to": "2025-12-31T23:59:59Z",
                "relation_effective_from": "2025-01-01T00:00:00Z",
                "relation_effective_to": "2025-12-31T23:59:59Z",
            },
            {
                "source_relation_id": "rel-1",
                "sibling_name": "Node 2025 / Relation 2026",
                "sibling_node_id": "sib-node-2025",
                "relation_properties": {"quota": 20},
                "sibling_effective_from": "2025-01-01T00:00:00Z",
                "sibling_effective_to": "2025-12-31T23:59:59Z",
                "relation_effective_from": "2026-01-01T00:00:00Z",
                "relation_effective_to": "2026-12-31T23:59:59Z",
            },
            {
                "source_relation_id": "rel-1",
                "sibling_name": "Relation 2026",
                "sibling_node_id": "sib-2026",
                "relation_properties": {"quota": 30},
                "sibling_effective_from": "2026-01-01T00:00:00Z",
                "sibling_effective_to": "2026-12-31T23:59:59Z",
                "relation_effective_from": "2026-01-01T00:00:00Z",
                "relation_effective_to": "2026-12-31T23:59:59Z",
            },
        ]

        with patch.object(reasoning_processing.Neo4jUtils, "run_query", return_value=records):
            result = reasoning_processing._find_sibling_relations(items, effective_from="2026", effective_to="2026")

        self.assertEqual(
            result,
            [
                {
                    "sibling_name": "Relation 2026",
                    "sibling_node_id": "sib-2026",
                    "relation_properties": {"quota": 30},
                }
            ],
        )

    def test_relation_reasoning_uses_source_siblings_and_triangle_nodes(self):
        tool_context = DummyToolContext()
        input_data = {
            "items": [
                {
                    "source_node_id": "src-1",
                    "relation_id": "rel-1",
                    "relation_label": "TRAINS",
                    "destination_node_id": "dst-1",
                    "source_node_attributes": ["name"],
                    "relation_attributes": ["quota"],
                }
            ]
        }
        step_1 = {"rel-1": {"fee": "1000"}}
        step_2_1 = [{"sibling_name": "Sibling 1", "sibling_node_id": "sib-1", "relation_properties": {}}]
        step_3 = [{"node_id": "tri-1", "name": "Triangle"}]

        with (
            patch.object(reasoning_processing, "_enrich_relation_attributes", return_value=step_1),
            patch.object(reasoning_processing, "_find_sibling_relations", return_value=step_2_1) as sibling_search,
            patch.object(reasoning_processing, "_find_triangle_nodes", return_value=step_3) as triangle_search,
            patch.object(reasoning_processing, "_enrich_source_node_attributes") as enrich_source,
        ):
            result = reasoning_processing.process_relation_reasoning(tool_context, input_data)

        normalized_items = reasoning_processing._normalize_relation_enrich_items(input_data["items"])
        sibling_search.assert_called_once_with(
            normalized_items,
            find_dest_sibling=False,
            effective_from=None,
            effective_to=None,
        )
        triangle_search.assert_called_once_with(normalized_items)
        enrich_source.assert_not_called()
        self.assertEqual(result["step_1_enriched_relation"], step_1)
        self.assertEqual(result["step_2_1_siblings"], step_2_1)
        self.assertEqual(result["step_2_2_enriched_source"], {})
        self.assertEqual(result["step_3_triangle_nodes"], step_3)
        self.assertEqual(json.loads(tool_context.state["data_lv2_data_enriched_relation"]), step_1)
        self.assertEqual(json.loads(tool_context.state["data_lv2_siblings_similar"]), step_2_1)
        self.assertEqual(json.loads(tool_context.state["data_lv2_data_enriched_nodes"]), {})
        self.assertEqual(json.loads(tool_context.state["data_lv2_related_nodes"]), step_3)

    def test_relation_reasoning_tries_destination_siblings_after_empty_source_result(self):
        tool_context = DummyToolContext()
        input_data = {
            "items": [
                {
                    "source_node_id": "src-1",
                    "relation_id": "rel-1",
                    "relation_label": "TRAINS",
                    "destination_node_id": "dst-1",
                    "source_node_attributes": ["name"],
                    "relation_attributes": ["quota"],
                }
            ]
        }
        step_2_1 = [{"sibling_name": "Sibling 2", "sibling_node_id": "sib-2", "relation_properties": {}}]

        with (
            patch.object(reasoning_processing, "_enrich_relation_attributes", return_value={}),
            patch.object(reasoning_processing, "_find_sibling_relations", side_effect=[[], step_2_1]) as sibling_search,
            patch.object(reasoning_processing, "_find_triangle_nodes", return_value=[]),
        ):
            result = reasoning_processing.process_relation_reasoning(tool_context, input_data)

        self.assertEqual(sibling_search.call_args_list[0].kwargs["find_dest_sibling"], False)
        self.assertEqual(sibling_search.call_args_list[0].kwargs["effective_from"], None)
        self.assertEqual(sibling_search.call_args_list[0].kwargs["effective_to"], None)
        self.assertEqual(sibling_search.call_args_list[1].kwargs["find_dest_sibling"], True)
        self.assertEqual(sibling_search.call_args_list[1].kwargs["effective_from"], None)
        self.assertEqual(sibling_search.call_args_list[1].kwargs["effective_to"], None)
        self.assertEqual(result["step_2_1_siblings"], step_2_1)

    def test_relation_reasoning_falls_back_to_source_enrichment_when_no_siblings_exist(self):
        tool_context = DummyToolContext()
        input_data = {
            "items": [
                {
                    "source_node_id": "src-1",
                    "relation_id": "rel-1",
                    "relation_label": "TRAINS",
                    "destination_node_id": "dst-1",
                    "source_node_attributes": ["name"],
                    "relation_attributes": ["quota"],
                }
            ]
        }
        step_2_2 = {"src-1": {"description": "extra"}}

        with (
            patch.object(reasoning_processing, "_enrich_relation_attributes", return_value={}),
            patch.object(reasoning_processing, "_find_sibling_relations", side_effect=[[], []]),
            patch.object(reasoning_processing, "_find_triangle_nodes") as triangle_search,
            patch.object(
                reasoning_processing, "_enrich_source_node_attributes", return_value=step_2_2
            ) as enrich_source,
        ):
            result = reasoning_processing.process_relation_reasoning(tool_context, input_data)

        triangle_search.assert_not_called()
        enrich_source.assert_called_once_with(
            reasoning_processing._normalize_relation_enrich_items(input_data["items"]), 5, 0.7
        )
        self.assertEqual(result["step_2_1_siblings"], [])
        self.assertEqual(result["step_2_2_enriched_source"], step_2_2)
        self.assertEqual(result["step_3_triangle_nodes"], [])


class PrepareReasoningInputTests(unittest.TestCase):
    def test_prepare_reasoning_input_accepts_list_and_deduplicates(self):
        result = prepare_reasoning_input(
            [
                {"node_id": "node-1", "attribute_name": "code"},
                {"node_id": "node-1", "attribute_name": "code"},
                {"node_id": "node-1", "attribute_name": "name"},
                {"node_id": None, "attribute_name": "skip"},
                "skip",
            ]
        )

        self.assertEqual(set(result["node-1"]), {"code", "name"})

    def test_prepare_reasoning_input_accepts_dict_and_skips_invalid_entries(self):
        result = prepare_reasoning_input(
            {
                "node-1": {"code": "C01", "name": "Program"},
                "node-2": ["skip"],
                "": {"ignored": True},
            }
        )

        self.assertEqual(set(result["node-1"]), {"code", "name"})
        self.assertNotIn("node-2", result)

    def test_prepare_reasoning_input_extracts_temporal_bounds_from_attributes(self):
        result = prepare_reasoning_input(
            {
                "node-1": {
                    "fee_information": "10M",
                    "effective_from": "2026",
                    "effective_to": "2026",
                }
            }
        )

        self.assertEqual(set(result["node-1"]["req_attrs"]), {"fee_information", "effective_from", "effective_to"})
        self.assertEqual(result["node-1"]["effective_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["node-1"]["effective_to"], "2026-12-31T23:59:59Z")

    def test_prepare_reasoning_input_accepts_explicit_temporal_bounds(self):
        result = prepare_reasoning_input(
            [{"node_id": "node-1", "attribute_name": "fee_information"}],
            effective_from="2026",
            effective_to="2026",
        )

        self.assertEqual(set(result["node-1"]["req_attrs"]), {"fee_information"})
        self.assertEqual(result["node-1"]["effective_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["node-1"]["effective_to"], "2026-12-31T23:59:59Z")

    def test_prepare_relation_enrich_input_filters_source_attrs_and_skips_malformed_candidates(self):
        result = prepare_relation_enrich_input(
            {
                "target-1": {
                    "candidates": [
                        {
                            "candidate_id": "source-1",
                            "relationship_id": "rel-1",
                            "relationship_type": "APPLIES_TO",
                            "candidate_properties": {
                                "id": "ignore",
                                "name": "Name",
                                "description": "Description",
                                "updated_at": "ignore",
                            },
                            "relationship_properties": {"quota": 10},
                        },
                        {
                            "candidate_id": "source-2",
                            "relationship_type": "APPLIES_TO",
                        },
                    ]
                }
            }
        )

        self.assertEqual(
            result,
            {
                "items": [
                    {
                        "source_node_id": "source-1",
                        "relation_id": "rel-1",
                        "relation_label": "APPLIES_TO",
                        "destination_node_id": "target-1",
                        "source_node_attributes": ["name", "description"],
                        "relation_attributes": ["quota"],
                    }
                ]
            },
        )

    def test_prepare_relation_enrich_input_keeps_temporal_outside_relation_items(self):
        result = prepare_relation_enrich_input(
            {
                "target-1": {
                    "candidates": [
                        {
                            "candidate_id": "source-1",
                            "relationship_id": "rel-1",
                            "relationship_type": "APPLIES_TO",
                            "candidate_properties": {"name": "Name"},
                            "relationship_properties": {
                                "quota": 10,
                                "effective_from": "2026",
                                "effective_to": "2027",
                            },
                        }
                    ]
                }
            },
            effective_from="2026",
            effective_to="2027",
        )

        self.assertEqual(result["effective_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["effective_to"], "2027-12-31T23:59:59Z")
        self.assertNotIn("effective_from", result["items"][0])
        self.assertNotIn("effective_to", result["items"][0])
        self.assertEqual(result["items"][0]["relation_attributes"], ["quota"])

    def test_prepare_relation_enrich_input_excludes_temporal_source_attrs_and_relation_id(self):
        result = prepare_relation_enrich_input(
            {
                "target-1": {
                    "candidates": [
                        {
                            "candidate_id": "source-1",
                            "relationship_id": "rel-1",
                            "relationship_type": "APPLIES_TO",
                            "candidate_properties": {
                                "name": "Name",
                                "effective_from": "2026",
                                "effective_to": "2027",
                                "status": "active",
                            },
                            "relationship_properties": {
                                "id": "meta-id",
                                "effective_from": "2026",
                                "quota": 10,
                            },
                        }
                    ]
                }
            }
        )

        self.assertEqual(result["items"][0]["source_node_attributes"], ["name", "status"])
        self.assertEqual(result["items"][0]["relation_attributes"], ["quota"])


class QueryPlanExecuteMixedPathTests(unittest.TestCase):
    def setUp(self):
        self.graph_query_builder = SimpleNamespace(config_base_path="/tmp/config")
        self.entity_resolver = object()
        self.orchestrator = QueryOrchestrator(
            entity_resolver=self.entity_resolver,
            graph_query_builder=self.graph_query_builder,
        )

    def test_mixed_path_forwards_node_reasoning_keys_from_mock_context(self):
        override_state = {
            "original_query": "test",
            "subtopics": [],
            "time_filter": None,
            "primary_entities": [{"label": "Major", "text": "CNTT"}],
            "topic": "known",
            "potential_entities": [],
            "qtype": "WHAT",
            "input_data": {"intent": "attributes"},
        }
        final_state = {
            "entity_attributes": {"node-1": {"code": "C01"}},
            "subgraph": {},
        }

        def _write_node_reasoning(tool_context, input_data, enrich_top_k, similarity_threshold):
            tool_context.state["data_lv2_data_enriched_nodes"] = '{"node-1": {"code": "C01"}}'
            tool_context.state["data_lv2_siblings_relative"] = '[{"name": "Sibling"}]'
            tool_context.state["data_lv2_siblings_similar"] = '[{"name": "Similar"}]'

        with (
            patch("services.query_plan_execute.FunctionFlowExecutor") as executor_cls,
            patch("services.query_plan_execute.prepare_reasoning_input", return_value={"node-1": ["code"]}),
            patch("services.query_plan_execute.process_reasoning", side_effect=_write_node_reasoning),
        ):
            executor_cls.return_value.execute_flow.return_value = final_state
            result = self.orchestrator.process_question_with_override(override_state, "flow")

        self.assertEqual(result["data_lv2_data_enriched_nodes"], '{"node-1": {"code": "C01"}}')
        self.assertEqual(result["data_lv2_siblings_relative"], '[{"name": "Sibling"}]')
        self.assertEqual(result["data_lv2_siblings_similar"], '[{"name": "Similar"}]')
        self.assertIsNone(result["data_lv2_data_enriched_relation"])
        self.assertIsNone(result["data_lv2_related_nodes"])

    def test_mixed_path_only_forwards_current_relation_keys(self):
        override_state = {
            "original_query": "test",
            "subtopics": [],
            "time_filter": None,
            "primary_entities": [{"label": "Major", "text": "CNTT"}],
            "topic": "known",
            "potential_entities": [],
            "qtype": "WHAT",
            "input_data": {"intent": "relation"},
        }
        final_state = {
            "subgraph": {"direct_connections": {"target-1": {"candidates": []}}},
        }

        def _write_relation_reasoning(tool_context, input_data, enrich_top_k=5, similarity_threshold=0.7):
            tool_context.state["data_lv2_data_enriched_relation"] = '{"rel-1": {"quota": 10}}'
            tool_context.state["data_lv2_siblings_similar"] = '[{"name": "Sibling"}]'
            tool_context.state["data_lv2_data_enriched_nodes"] = '{"src-1": {"description": "extra"}}'
            tool_context.state["data_lv2_related_nodes"] = '[{"node_id": "tri-1"}]'

        with (
            patch("services.query_plan_execute.FunctionFlowExecutor") as executor_cls,
            patch(
                "services.query_plan_execute.prepare_relation_enrich_input",
                return_value={"items": [{"relation_id": "rel-1"}]},
            ),
            patch("services.query_plan_execute.process_relation_reasoning", side_effect=_write_relation_reasoning),
        ):
            executor_cls.return_value.execute_flow.return_value = final_state
            result = self.orchestrator.process_question_with_override(override_state, "flow")

        self.assertEqual(result["data_lv2_data_enriched_relation"], '{"rel-1": {"quota": 10}}')
        self.assertEqual(result["data_lv2_related_nodes"], '[{"node_id": "tri-1"}]')
        self.assertIsNone(result["data_lv2_siblings_similar"])
        self.assertIsNone(result["data_lv2_data_enriched_nodes"])

    def test_mixed_path_returns_canonical_query_results_for_attributes(self):
        override_state = {
            "original_query": "học phí CNTT",
            "subtopics": [],
            "time_filter": None,
            "primary_entities": [{"label": "Major", "text": "CNTT"}],
            "topic": "known",
            "potential_entities": [],
            "qtype": "WHAT",
            "input_data": {
                "intent": "attributes",
                "count_enumerate_targets": [],
            },
        }
        final_state = {
            "primary_found_list": [
                {"node_id": "major-1", "node_name": "Công nghệ thông tin", "node_type": "Major", "score": 0.98}
            ],
            "entity_attributes": {"major-1": {"name": "Công nghệ thông tin", "tuition_fee": "31 triệu/năm"}},
            "candidate_pool": {"items": [{"id": "noise"}]},
            "matched_attr_results": [{"noise": True}],
        }

        with patch("services.query_plan_execute.FunctionFlowExecutor") as executor_cls:
            executor_cls.return_value.execute_flow.return_value = final_state
            result = self.orchestrator.process_question_with_override(override_state, "what_attributes")

        query_results = result["query_results"]
        self.assertEqual(
            sorted(query_results.keys()),
            ["answer", "entities", "evidence", "intent", "meta", "pagination", "question_family", "status"],
        )
        self.assertEqual(query_results["question_family"], "what")
        self.assertEqual(query_results["intent"], "attributes")
        self.assertEqual(query_results["answer"]["kind"], "attributes")
        self.assertEqual(query_results["status"], "ok")
        self.assertEqual(query_results["entities"]["primary"][0]["id"], "major-1")
        self.assertNotIn("candidate_pool", query_results)
        self.assertNotIn("matched_attr_results", query_results)

    def test_mixed_path_returns_canonical_pagination_for_count_queries(self):
        override_state = {
            "original_query": "có bao nhiêu ngành",
            "subtopics": [],
            "time_filter": None,
            "primary_entities": [],
            "context_entities": [{"label": "University", "text": "GDU"}],
            "topic": "known",
            "potential_entities": [],
            "qtype": "WHAT",
            "input_data": {
                "intent": "count",
                "count_enumerate_targets": ["Major"],
                "primary_entities": [],
                "keywords": [],
            },
        }
        final_state = {
            "context_found_list": [{"node_id": "uni-1", "node_name": "GDU", "node_type": "University", "score": 0.95}],
            "list_results": {
                "items": [{"id": "major-1", "name": "CNTT", "labels": ["Major"]}],
                "total": 12,
                "display_limit": 5,
                "start_index": 0,
                "list_last_index": 5,
            },
        }

        with patch("services.query_plan_execute.FunctionFlowExecutor") as executor_cls:
            executor_cls.return_value.execute_flow.return_value = final_state
            result = self.orchestrator.process_question_with_override(override_state, "what_list")

        query_results = result["query_results"]
        self.assertEqual(query_results["intent"], "count")
        self.assertEqual(query_results["answer"]["kind"], "collection")
        self.assertEqual(query_results["answer"]["data"]["count_mode"], "simple")
        self.assertEqual(query_results["answer"]["data"]["total"], 12)
        self.assertEqual(query_results["pagination"]["total"], 12)
        self.assertEqual(query_results["pagination"]["display_limit"], 5)
        self.assertEqual(query_results["pagination"]["next_start_index"], 5)


class QueryPlanExecuteSinglePathTests(unittest.TestCase):
    def setUp(self):
        self.graph_query_builder = SimpleNamespace(config_base_path="/tmp/config")
        self.entity_resolver = object()
        self.orchestrator = QueryOrchestrator(
            entity_resolver=self.entity_resolver,
            graph_query_builder=self.graph_query_builder,
        )

    def test_single_path_keeps_resolved_primary_entities_for_what_attributes(self):
        tool_context = SimpleNamespace(
            state=DummyState(
                {
                    "original_query": "Giới thiệu cho tôi ngành Big Data",
                    "subtopics": ["career", "curriculum", "outcomes"],
                    "time": {"from_year": 2026, "to_year": None},
                    "time_filter": None,
                    "primary_entities": [{"label": "Major", "text": "Big Data"}],
                    "context_entities": [{"label": "University", "text": "Trường Đại học Gia Định"}],
                    "topic": "known",
                    "potential_entities": [],
                    "qtype": "WHAT",
                    "input_data": {
                        "intent": "attributes",
                        "count_enumerate_targets": [],
                        "subtopics": ["career", "curriculum", "outcomes"],
                        "time": {"from_year": 2026, "to_year": None},
                        "primary_entities": [{"label": "Major", "text": "Big Data"}],
                        "context_entities": [{"label": "University", "text": "Trường Đại học Gia Định"}],
                    },
                    "extra_data": {},
                }
            )
        )
        final_state = {
            "primary_found_list": [
                {
                    "node_id": "major-1",
                    "node_name": "Khai thác dữ liệu lớn",
                    "node_type": "Major",
                    "score": 0.99,
                }
            ],
            "entity_attributes": {
                "major-1": {"name": "Khai thác dữ liệu lớn", "description": "Ngành học về dữ liệu lớn"}
            },
        }

        with (
            patch("services.query_plan_execute.FunctionFlowExecutor") as executor_cls,
            patch("services.query_plan_execute.prepare_reasoning_input", return_value=None),
        ):
            executor_cls.return_value.execute_flow.return_value = final_state
            self.orchestrator.process_question(tool_context, "what_attributes")

        query_results = tool_context.state["query_results"]
        self.assertEqual(query_results["intent"], "attributes")
        self.assertEqual(query_results["entities"]["primary"][0]["id"], "major-1")
        self.assertEqual(query_results["answer"]["data"]["subject"]["id"], "major-1")


class WhatQueryResultNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = QueryOrchestrator(
            entity_resolver=object(),
            graph_query_builder=SimpleNamespace(config_base_path="/tmp/config"),
        )

    def test_normalize_compare_uses_academic_program_fallback_shape(self):
        final_state = {
            "primary_found_list": [{"node_id": "m1", "node_name": "RHM", "node_type": "Major"}],
            "compare_found_list": [{"node_id": "m2", "node_name": "Y khoa", "node_type": "Major"}],
            "primary_academic_program_attributes": {"ap1": {"name": "RHM", "total_credits": 150}},
            "compare_academic_program_attributes": {"ap2": {"name": "Y khoa", "total_credits": 180}},
        }

        result = self.orchestrator._normalize_what_query_results("what_compare", {"intent": "compare"}, final_state)

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["meta"]["source"], "hybrid")
        self.assertEqual(result["answer"]["kind"], "comparison")
        self.assertEqual(result["answer"]["data"]["summary_basis"], "academic_program_fallback")
        self.assertEqual(len(result["answer"]["data"]["compared_attributes"]), 2)

    def test_normalize_relation_uses_compact_context_without_subgraph(self):
        final_state = {
            "context_found_list": [{"node_id": "u1", "node_name": "GDU", "node_type": "University"}],
            "primary_found_list": [{"node_id": "p1", "node_name": "Hiệu trưởng", "node_type": "Person"}],
            "relation_context": {
                "context": {"id": "u1", "name": "GDU", "type": "University"},
                "total_candidates": 1,
                "candidates": [
                    {
                        "id": "person-1",
                        "name": "Nguyễn Văn A",
                        "relationships": [{"type": "WORKS_IN", "attributes": {"role": "Hiệu trưởng"}}],
                    }
                ],
            },
            "subgraph": {"direct_connections": {"noise": {}}},
        }

        result = self.orchestrator._normalize_what_query_results("what_relation", {"intent": "relation"}, final_state)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"]["kind"], "relation")
        self.assertEqual(result["answer"]["data"]["relations"], ["WORKS_IN"])
        self.assertNotIn("subgraph", json.dumps(result))

    def test_normalize_list_and_count_keep_connected_via_in_items(self):
        final_state = {
            "context_found_list": [{"node_id": "faculty-1", "node_name": "Khoa CNTT", "node_type": "Faculty"}],
            "list_results": {
                "items": [
                    {
                        "id": "course-1",
                        "name": "Giải tích 1",
                        "labels": ["Course"],
                        "connected_via": ["Ngành Công nghệ thông tin"],
                    }
                ],
                "total": 3,
                "display_limit": 2,
                "start_index": 0,
                "list_last_index": 2,
            },
        }

        for intent in ("list", "count"):
            with self.subTest(intent=intent):
                result = self.orchestrator._normalize_what_query_results(
                    "what_list",
                    {"intent": intent, "count_enumerate_targets": ["Course"]},
                    final_state,
                )

                self.assertEqual(result["intent"], intent)
                self.assertEqual(
                    result["answer"]["data"]["items"][0]["connected_via"],
                    ["Ngành Công nghệ thông tin"],
                )

    def test_normalize_how_admission_returns_canonical_procedure_shape(self):
        all_flow_results = {
            "Ngành Marketing": {
                "resolved_entity": {
                    "entity_id": "major-1",
                    "entity_name": "Ngành Marketing",
                    "entity_type": "Major",
                    "similarity_score": 0.97,
                },
                "traversal_results": {
                    "admission_policy": {
                        "data": [
                            {
                                "name": "Đề án tuyển sinh 2026",
                                "registration_procedure": "Nộp hồ sơ online",
                                "admission_portal": "https://tuyensinh.gdu.edu.vn",
                            }
                        ]
                    },
                    "admission_methods": {"data": [{"name": "Xét học bạ", "selection_method": "Điểm trung bình THPT"}]},
                    "admission_combinations": {"data": [{"code": "A01", "name": "Toán-Lý-Anh"}]},
                    "major_quotas_scores": {"data": [{"major_name": "Ngành Marketing", "quota": 120}]},
                },
            }
        }

        result = self.orchestrator._normalize_how_query_results("how_admission", all_flow_results)

        self.assertEqual(result["question_family"], "how")
        self.assertEqual(result["intent"], "admission")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["answer"]["kind"], "procedure")
        self.assertEqual(result["answer"]["data"]["scope_entity"]["id"], "major-1")
        self.assertEqual(result["answer"]["data"]["policy"]["name"], "Đề án tuyển sinh 2026")
        self.assertEqual(len(result["answer"]["data"]["methods"]), 1)
        self.assertEqual(result["meta"]["source"], "graph")

    def test_normalize_how_empty_uses_crawled_fallback(self):
        result = self.orchestrator._normalize_how_query_results(
            "how_skill_to_major",
            all_flow_results={},
            crawled_data="Thông tin tham khảo từ nguồn web",
        )

        self.assertEqual(result["question_family"], "how")
        self.assertEqual(result["intent"], "skill_to_major")
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["answer"]["kind"], "eligibility")
        self.assertEqual(result["meta"]["source"], "crawled")
        self.assertTrue(result["meta"]["used_fallback"])
        self.assertIn("crawled_data", result["evidence"]["fallback"])

    def test_prompt_builder_treats_canonical_empty_how_result_as_empty(self):
        query_results = {
            "question_family": "how",
            "intent": "course",
            "status": "empty",
            "entities": {"primary": [], "context": []},
            "answer": {"kind": "roadmap", "data": {}},
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
            "meta": {"source": "graph", "used_fallback": False},
        }

        self.assertTrue(_is_answer_empty(query_results))

    def test_compact_prompt_block_content_prunes_how_internal_noise(self):
        query_results = {
            "question_family": "how",
            "intent": "admission",
            "status": "ok",
            "answer": {
                "kind": "procedure",
                "data": {
                    "scope_entity": {
                        "id": "major-1",
                        "name": "Ngành Marketing",
                        "type": "Major",
                        "score": 0.97,
                    },
                    "policy": {
                        "name": "Đề án tuyển sinh 2026",
                        "admission_portal": "https://tuyensinh.gdu.edu.vn",
                        "effective_from": "2026-01-01T00:00:00Z",
                    },
                    "methods": [{"name": "Xét học bạ", "source_branch": "graph"}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
            "meta": {"source": "graph", "used_fallback": False},
        }

        compacted = _compact_prompt_block_content(query_results, query_results=True)

        self.assertEqual(compacted["question_family"], "how")
        self.assertEqual(compacted["answer"]["kind"], "procedure")
        self.assertEqual(compacted["answer"]["data"]["scope_entity"]["name"], "Ngành Marketing")
        self.assertEqual(compacted["answer"]["data"]["policy"]["name"], "Đề án tuyển sinh 2026")
        self.assertEqual(compacted["answer"]["data"]["methods"][0]["name"], "Xét học bạ")
        self.assertNotIn("id", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("score", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("effective_from", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("source_branch", json.dumps(compacted, ensure_ascii=False))

    def test_compact_prompt_block_content_keeps_fallback_crawled_data_for_how(self):
        query_results = {
            "question_family": "how",
            "intent": "admission",
            "status": "fallback",
            "answer": {"kind": "procedure"},
            "evidence": {
                "structured": {},
                "fallback": {
                    "crawled_data": {
                        "summary": "Bạn có thể theo dõi website để cập nhật mốc tuyển sinh mới nhất.",
                    }
                },
                "media": {},
            },
            "meta": {"source": "crawled", "used_fallback": True},
        }

        compacted = _compact_prompt_block_content(query_results, query_results=True)

        self.assertEqual(compacted["status"], "fallback")
        self.assertEqual(compacted["meta"]["source"], "crawled")
        self.assertEqual(
            compacted["evidence"]["fallback"]["crawled_data"]["summary"],
            "Bạn có thể theo dõi website để cập nhật mốc tuyển sinh mới nhất.",
        )

    def test_prompt_builder_treats_canonical_empty_what_result_as_empty(self):
        query_results = {
            "question_family": "what",
            "intent": "list",
            "status": "empty",
            "entities": {"primary": [], "context": [], "compare": []},
            "answer": {"kind": "collection", "data": {}},
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
            "pagination": {"total": 0, "display_limit": 0, "next_start_index": 0},
            "meta": {"source": "graph", "used_fallback": False},
        }

        self.assertTrue(_is_answer_empty(query_results))

    def test_compact_prompt_block_content_prunes_what_attribute_noise(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {
                        "id": "4f8ebbe8-e7cd-4a6e-93f6-399d4e43672b",
                        "name": "Ngành Marketing",
                        "type": "Major",
                        "description": "Mô tả ngành Marketing",
                        "score": 0.99,
                    },
                    "attributes": [
                        {
                            "node_id": "4f8ebbe8-e7cd-4a6e-93f6-399d4e43672b",
                            "values": {
                                "name": "Ngành Marketing",
                                "description": "Mô tả ngành Marketing",
                                "key_points": "Điểm nổi bật",
                                "career_opportunities": ["Chuyên viên Marketing"],
                                "score": 0.88,
                            },
                        }
                    ],
                    "relation_attributes": [],
                    "media_available": False,
                },
            },
            "evidence": {
                "structured": {
                    "branch": "entity_attributes",
                    "attributes": [
                        {
                            "node_id": "4f8ebbe8-e7cd-4a6e-93f6-399d4e43672b",
                            "values": {
                                "name": "Ngành Marketing",
                                "key_points": "Điểm nổi bật",
                            },
                        }
                    ],
                },
                "fallback": {},
                "media": {},
            },
            "pagination": {"total": 0, "display_limit": 0, "next_start_index": 0},
            "meta": {"source": "graph", "used_fallback": False},
        }

        compacted = _compact_prompt_block_content(query_results, query_results=True)

        self.assertEqual(compacted["answer"]["data"]["subject"]["name"], "Ngành Marketing")
        self.assertNotIn("id", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("score", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("branch", json.dumps(compacted, ensure_ascii=False))
        self.assertNotIn("media_available", compacted["answer"]["data"])
        self.assertEqual(
            compacted["answer"]["data"]["attributes"],
            [{"values": {"key_points": "Điểm nổi bật", "career_opportunities": ["Chuyên viên Marketing"]}}],
        )

    def test_compact_prompt_block_content_keeps_connected_via_for_what_list_and_count(self):
        for intent in ("list", "count"):
            with self.subTest(intent=intent):
                query_results = {
                    "question_family": "what",
                    "intent": intent,
                    "status": "ok",
                    "answer": {
                        "kind": "collection",
                        "data": {
                            "items": [
                                {
                                    "id": "course-1",
                                    "name": "Giải tích 1",
                                    "labels": ["Course"],
                                    "connected_via": ["Ngành Công nghệ thông tin"],
                                }
                            ],
                            "total": 3,
                            "count_mode": "simple" if intent == "count" else None,
                        },
                    },
                    "evidence": {"structured": {}, "fallback": {}, "media": {}},
                    "pagination": {"total": 3, "display_limit": 2, "next_start_index": 2},
                    "meta": {"source": "graph", "used_fallback": False},
                }

                compacted = _compact_prompt_block_content(query_results, query_results=True)

                self.assertEqual(
                    compacted["answer"]["data"]["items"][0]["connected_via"],
                    ["Ngành Công nghệ thông tin"],
                )
                self.assertEqual(compacted["answer"]["data"]["items"][0]["type"], "Course")
                self.assertNotIn("id", json.dumps(compacted, ensure_ascii=False))

    def test_answer_query_prompt_compacts_relation_and_support_blocks(self):
        query_results = {
            "question_family": "what",
            "intent": "relation",
            "status": "ok",
            "answer": {
                "kind": "relation",
                "data": {
                    "subject": {"id": "u1", "name": "GDU", "type": "University"},
                    "object": {"id": "p1", "name": "Nguyễn Văn A", "type": "Person"},
                    "relations": ["WORKS_IN"],
                    "candidates": [
                        {
                            "id": "person-1",
                            "name": "Nguyễn Văn A",
                            "connected_via": ["Khoa Quản trị"],
                            "relationships": [
                                {
                                    "type": "WORKS_IN",
                                    "attributes": {"role": "Hiệu trưởng"},
                                    "source_node_id": "u1",
                                }
                            ],
                        }
                    ],
                },
            },
            "evidence": {
                "structured": {"context": {"id": "u1"}, "candidates": [{"id": "person-1"}]},
                "fallback": {},
                "media": {},
            },
            "meta": {"source": "graph", "used_fallback": False},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
            data_lv2_data_enriched_relation={
                "rel-1": {
                    "quota": 10,
                    "effective_from": "2026-01-01T00:00:00Z",
                    "relation_id": "5:abc:123",
                }
            },
            data_lv2_related_nodes=[{"node_id": "tri-1", "name": "Khoa Quản trị", "type": "Faculty", "score": 0.7}],
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn('"relations": [', prompt)
        self.assertIn('"WORKS_IN"', prompt)
        self.assertIn('"role": "Hiệu trưởng"', prompt)
        self.assertIn('"connected_via": [', prompt)
        self.assertIn('"Khoa Quản trị"', prompt)
        self.assertIn('"quota": 10', prompt)
        self.assertIn('"name": "Khoa Quản trị"', prompt)
        self.assertNotIn('"id": "u1"', prompt)
        self.assertNotIn('"node_id": "tri-1"', prompt)
        self.assertNotIn("effective_from", prompt)
        self.assertNotIn("relation_id", prompt)
        self.assertNotIn("score", prompt)

    def test_compact_prompt_block_content_keeps_fallback_crawled_data_for_what_relation(self):
        query_results = {
            "question_family": "what",
            "intent": "relation",
            "status": "fallback",
            "answer": {"kind": "relation"},
            "evidence": {
                "structured": {},
                "fallback": {
                    "crawled_data": {
                        "summary": "Ngành Kỹ thuật phần mềm hiện chưa hỗ trợ học từ xa.",
                        "sources": [
                            {
                                "title": "Thông tin tuyển sinh",
                                "url": "https://example.edu/ktpm",
                            }
                        ],
                    }
                },
                "media": {},
            },
            "pagination": {"total": 0, "display_limit": 0, "next_start_index": 0},
            "meta": {"source": "crawled", "used_fallback": True},
        }

        compacted = _compact_prompt_block_content(query_results, query_results=True)

        self.assertEqual(compacted["status"], "fallback")
        self.assertEqual(compacted["meta"]["source"], "crawled")
        self.assertIn("evidence", compacted)
        self.assertIn("fallback", compacted["evidence"])
        self.assertEqual(
            compacted["evidence"]["fallback"]["crawled_data"]["summary"],
            "Ngành Kỹ thuật phần mềm hiện chưa hỗ trợ học từ xa.",
        )

    def test_answer_query_prompt_omits_reference_mappings_when_irrelevant(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Chuyên ngành Lập trình kết nối vạn vật"},
                    "attributes": [{"values": {"name": "Chuyên ngành Lập trình kết nối vạn vật"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
        )

        prompt = build_answer_query_prompt(state)

        self.assertNotIn("K-Year Mapping(academic_cohort)", prompt)

    def test_answer_query_prompt_includes_reference_mappings_for_cohort_queries(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Khoá K20"},
                    "attributes": [{"values": {"name": "Khoá K20"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            latest_user_input="Khoá K20 là năm nào?",
            query_results=query_results,
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn("K-Year Mapping(academic_cohort)", prompt)

    def test_adk_prompt_uses_shorter_response_structure_wording(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Ngành Công nghệ thông tin"},
                    "attributes": [{"values": {"name": "Ngành Công nghệ thông tin"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = ResponsePolishingPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
            playbook_answer_guidance="Hỏi thêm về định hướng học tập",
            question_explore_deep_hint="Gợi ý câu hỏi",
        )

        prompt = build_response_polishing_prompt(state)

        self.assertIn("Generate only the components that have data, in this order:", prompt)
        self.assertIn("If `PRIMARY_FACTS` exists, answer it before any playbook or action block.", prompt)
        self.assertNotIn("MENTAL MODEL", prompt)
        self.assertNotIn("VIOLATION DETECTION", prompt)
        self.assertNotIn("UNIVERSAL STRICT RELEVANCE CHECK", prompt)
        self.assertNotIn("RELEVANCE FILTERING (CRITICAL)", prompt)

    def test_counselor_playbook_prompt_keeps_rules_without_verbose_examples(self):
        state = CounselorPlaybookPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            user_role="parent",
            playbook_answer_guidance="Thu thập họ tên, số điện thoại, email, trường THPT",
        )

        prompt = build_counselor_playbook_prompt(state)

        self.assertIn("Do not answer factual questions here.", prompt)
        self.assertIn(
            "If guidance asks for student info in parent context, ask for the student's info, not the parent's.", prompt
        )
        self.assertNotIn("BAD", prompt)
        self.assertNotIn("GOOD", prompt)
        self.assertNotIn("Angle 1", prompt)

    def test_answer_query_prompt_uses_shorter_follow_up_data_role_rules(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Ngành Công nghệ thông tin"},
                    "attributes": [{"values": {"name": "Ngành Công nghệ thông tin"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
            question_explore_deep_hint="Gợi ý câu hỏi",
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn("Build questions only from available PRIMARY_FACTS / ENRICHED / SIBLINGS / RELATED data.", prompt)
        self.assertNotIn("DATA-DRIVEN QUESTIONS (CRITICAL)", prompt)
        self.assertNotIn("QUESTION STYLE (ABSOLUTE RULE", prompt)

    def test_answer_query_prompt_keeps_follow_up_hints_when_crawled_is_primary(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "empty",
            "answer": {"kind": "attributes", "data": {}},
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
            data_crawled={"title": "Thông tin tham khảo"},
            question_explore_deep_hint="Gợi ý câu hỏi",
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn("PRIMARY source (promoted — no graph data found for this section).", prompt)
        self.assertIn("## FOLLOW_UP_HINTS", prompt)

    def test_answer_query_prompt_requires_enriched_data_and_impersonal_hints(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Ngành Trí tuệ nhân tạo"},
                    "attributes": [{"values": {"name": "Ngành Trí tuệ nhân tạo"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            query_results=query_results,
            data_lv2_data_enriched_nodes={"domain": "Công nghệ thông tin", "status": "official"},
            question_explore_deep_hint="Gợi ý câu hỏi",
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn("If this block adds new facts beyond PRIMARY_FACTS, include those facts in the answer.", prompt)
        self.assertIn(
            "The only allowed ending beyond factual content is the final `FOLLOW_UP_HINTS` numbered list when that block exists.",
            prompt,
        )
        self.assertIn("end once with a numbered list of impersonal topic questions.", prompt)

    def test_answer_query_prompt_renders_internal_data_as_markdown_sections(self):
        query_results = {
            "question_family": "what",
            "intent": "attributes",
            "status": "ok",
            "answer": {
                "kind": "attributes",
                "data": {
                    "subject": {"name": "Ngành Trí tuệ nhân tạo"},
                    "attributes": [{"values": {"name": "Ngành Trí tuệ nhân tạo"}}],
                },
            },
            "evidence": {"structured": {}, "fallback": {}, "media": {}},
        }
        state = AnswerQueryPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            is_query=True,
            latest_user_input="Tư vấn ngành AI",
            query_results=query_results,
            data_lv2_data_enriched_nodes={"domain": "Công nghệ thông tin"},
            question_explore_deep_hint="Gợi ý câu hỏi",
        )

        prompt = build_answer_query_prompt(state)

        self.assertIn("# 1. PRIORITY ORDER", prompt)
        self.assertIn("# 4. DYNAMIC CONTEXT", prompt)
        self.assertIn("## PRIMARY_FACTS", prompt)
        self.assertIn("**Purpose:**", prompt)
        self.assertIn("**Rules**", prompt)
        self.assertIn("```json", prompt)
        self.assertIn("```text", prompt)
        self.assertNotIn("DATA_ROLE:", prompt)
        self.assertNotIn("\n PURPOSE:", prompt)
        self.assertNotIn("\n DATA:", prompt)

    def test_counselor_playbook_prompt_renders_internal_data_as_markdown_sections(self):
        state = CounselorPlaybookPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            playbook_answer_guidance="Hỏi thêm về định hướng học tập",
            playbook_action_results=["Lựa chọn 1", "Lựa chọn 2"],
        )

        prompt = build_counselor_playbook_prompt(state)

        self.assertIn("# 1. PRIORITY ORDER", prompt)
        self.assertIn("# 4. DYNAMIC CONTEXT", prompt)
        self.assertIn("## ACTION_DATA", prompt)
        self.assertIn("## PLAYBOOK_INTENT", prompt)
        self.assertIn("**Data**", prompt)
        self.assertIn("```text", prompt)
        self.assertNotIn("DATA_ROLE:", prompt)

    def test_counselor_playbook_prompt_treats_history_as_reference_only(self):
        state = CounselorPlaybookPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            playbook_answer_guidance="Mời bạn xác nhận thông tin hồ sơ để tiếp tục.",
            conversation_turns=[
                {
                    "user_message": "THPT Diên Hồng tphcm",
                    "counselor_playbook_response": "Bạn chọn trường theo số thứ tự nha.",
                }
            ],
        )

        prompt = build_counselor_playbook_prompt(state)

        self.assertIn("Playbook (REFERENCE ONLY): Bạn chọn trường theo số thứ tự nha.", prompt)
        self.assertIn("History & Context Analysis.", prompt)
        self.assertIn("Historical answers/playbook messages here are REFERENCE ONLY, never factual evidence.", prompt)

    def test_counselor_playbook_prompt_forbids_history_from_overriding_current_playbook(self):
        state = CounselorPlaybookPromptState(
            self_pronoun="mình",
            user_pronoun="bạn",
            playbook_answer_guidance="Mời bạn xác nhận thông tin hồ sơ để tiếp tục.",
            playbook_action_results=None,
            conversation_turns=[
                {
                    "user_message": "THPT Diên Hồng tphcm",
                    "counselor_playbook_response": "1. THPT Nguyễn Du\n2. THPT Đào Duy Từ\nBạn chọn trường theo số thứ tự nha.",
                }
            ],
        )

        prompt = build_counselor_playbook_prompt(state)

        self.assertIn(
            "If conversation history conflicts with current `PLAYBOOK_GUIDELINE`, `PLAYBOOK_INTENT`, or `ACTION_DATA`, ignore the history and follow the current playbook data.",
            prompt,
        )
        self.assertIn(
            "Never revive old school lists, old confirmation prompts, or old options from history when current `ACTION_DATA` does not contain them.",
            prompt,
        )
        self.assertIn(
            "`PLAYBOOK_GUIDELINE`, `PLAYBOOK_INTENT`, and `ACTION_DATA` are the active source of truth for the current response. `LATEST_TURN_CONTEXT` is reference-only.",
            prompt,
        )
        self.assertIn(
            "If current `ACTION_DATA` has no school list, do not reconstruct or repeat any old school list from history.",
            prompt,
        )

    def test_answer_query_prompt_state_rejects_playbook_fields(self):
        with self.assertRaises(TypeError):
            AnswerQueryPromptState(
                self_pronoun="mình",
                user_pronoun="bạn",
                playbook_answer_guidance="Không nên tồn tại trong answer_query_agent",
            )

    def test_counselor_playbook_prompt_state_rejects_query_fields(self):
        with self.assertRaises(TypeError):
            CounselorPlaybookPromptState(
                self_pronoun="mình",
                user_pronoun="bạn",
                query_results={"status": "ok"},
            )

    def test_what_query_result_schema_validates_intent_answer_kind_pair(self):
        with self.assertRaises(ValueError):
            WhatQueryResult(
                intent=WhatIntent.RELATION,
                answer=WhatAnswer(kind=WhatAnswerKind.COLLECTION),
            )

    def test_what_query_result_schema_auto_marks_crawled_as_fallback(self):
        result = WhatQueryResult(
            intent=WhatIntent.LIST,
            status="fallback",
            answer=WhatAnswer(kind=WhatAnswerKind.COLLECTION),
            meta=WhatMeta(source=WhatSource.CRAWLED, used_fallback=False),
        )

        self.assertTrue(result.meta.used_fallback)


class _DummyState(dict):
    def to_dict(self):
        return dict(self)


class _DummyProcessorContext:
    def __init__(self, initial_state=None):
        self.state = _DummyState(initial_state or {})
        self.actions = SimpleNamespace(skip_summarization=False)


class _DummyInputTool(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class QueryAgentCommonDataTests(unittest.TestCase):
    def _build_input_tool(self, original_query: str) -> _DummyInputTool:
        return _DummyInputTool(
            original_query=original_query,
            is_query=True,
            question_type="WHAT",
            intent="attribute",
            primary_topic="Major",
            primary_entities=[],
            context_entities=[],
            compare_mode=False,
            compare_targets=[],
            keywords=[],
            subtopics=[],
            ambiguity_level="low",
            needs_disambiguation=False,
            time=None,
            time_compare=None,
            potential_entities=[],
            count_enumerate_targets=[],
        )

    def test_query_internal_data_short_circuits_before_dmn_when_common_data_found(self):
        context = _DummyProcessorContext()
        input_tool = self._build_input_tool("học phí ngành CNTT là bao nhiêu?")
        common_hits = [{"content": "Thông tin học phí", "score": 0.93}]

        with (
            patch("tools.query_agent.search_common_qa", return_value=common_hits),
            patch("tools.query_agent.QueryPlanDMNProcessor") as processor_cls,
        ):
            result = query_internal_data(context, input_tool, return_result=True)

        processor_cls.assert_not_called()
        stored = json.loads(context.state["query_results"])
        self.assertEqual(stored["question"], input_tool.original_query)
        self.assertEqual(stored["answer"], common_hits)
        self.assertEqual(stored["source"], "knowledge_common_qa")
        self.assertEqual(json.loads(result["query_results"])["answer"], common_hits)

    def test_query_internal_data_runs_existing_dmn_flow_when_common_data_empty(self):
        context = _DummyProcessorContext()
        input_tool = self._build_input_tool("ngành CNTT học gì")

        with (
            patch("tools.query_agent.search_common_qa", return_value=[]),
            patch("tools.query_agent.QueryPlanDMNProcessor") as processor_cls,
        ):
            processor_cls.return_value.process_query_plan.side_effect = lambda context: context.state.update(
                {"query_results": "from dmn", "answer_plan": "answer plan"}
            )
            result = query_internal_data(context, input_tool, return_result=True)

        processor_cls.assert_called_once()
        processor_cls.return_value.process_query_plan.assert_called_once()
        self.assertEqual(context.state["query_results"], "from dmn")
        self.assertEqual(result["query_results"], "from dmn")
        self.assertEqual(result["answer_plan"], "answer plan")


if __name__ == "__main__":
    unittest.main()
