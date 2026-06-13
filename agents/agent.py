"""Admission Root Agent - Multi-Agent Orchestrator.

Main coordinator using ADK ParallelAgent + SequentialAgent:
- Query Analyzer Agent: Handle admission questions (parallel)
- Data Collector Agent: Gather customer information (parallel)
- Answer Query Agent: Data-driven answer (parallel response)
- Counselor Playbook Agent: Playbook-driven interaction (parallel response)

Architecture: SequentialAgent(ParallelAgent[query+data+segment] + ParallelAgent[answer+playbook])
"""

import logging

from google.adk.agents.parallel_agent import ParallelAgent
from google.adk.agents.sequential_agent import SequentialAgent

from agents.answer_query_agent import answer_query_agent
from agents.counselor_playbook_agent import counselor_playbook_agent
from agents.data_collector_agent import data_collector_agent_for_root

# import child agents
from agents.message_rewrite_agent import message_rewrite_agent
from agents.parent_intent_analyzer_agent import parent_intent_analyzer_agent
from agents.query_agent import query_analyzer_agent
from agents.segment_agent import segment_agent
from configs.config_service import get_settings

# Setup logging
logger = logging.getLogger(__name__)

settings = get_settings()

logger.info(" Admission Root Agent - Multi-Agent Orchestrator")
logger.info(
    "   Architecture: ParentIntentAnalyzer → MessageRewrite → ParallelAgent (Query + Data Collection) → ParallelAgent (Answer + Playbook)"
)

parallel_processing_agent = ParallelAgent(
    name="parallel_processing_agent",
    sub_agents=[query_analyzer_agent,
                # data_collector_agent_for_root,
                # segment_agent
                ],
    description="Runs Query Analyzer and Data Collector in parallel",
)

response_agents = ParallelAgent(
    name="response_agents",
    sub_agents=[
        answer_query_agent,
        # counselor_playbook_agent,
    ],
    description="Runs Answer Query and Counselor Playbook in parallel",
)

SequeAgent = SequentialAgent(
    name="sequential_processing_agent",
    sub_agents=[
        # parent_intent_analyzer_agent,  # Step 1: Classify UC1/UC2/SPAM/UNKNOWN — sets is_admission_topic
        message_rewrite_agent,  # Step 2: Rewrite implicit questions into explicit ones (skipped if UC2)
        parallel_processing_agent,  # Step 3: Query + Data + Segment (skipped if UC2)
        response_agents,  # Step 4: Answer + Playbook (skipped if UC2, except transfer msg)
    ],
    description="Runs ParentIntentAnalyzer → MessageRewrite → Parallel Processing → Parallel Response agents",
)


# Build Root Agent
def build_root_agent() -> SequentialAgent:
    """Build Admission Root Agent with Parallel Processing and Parallel Response agents.

    Returns:
            SequentialAgent running Query Analyzer + Data Collector → Answer + Playbook
    """
    return SequeAgent


logger.info(" Admission Root Agent initialized")
logger.info("Architecture: SequentialAgent(ParallelAgent[processing] + ParallelAgent[response])")
logger.info("=" * 80)

# Export for ADK framework
root_agent = build_root_agent()
