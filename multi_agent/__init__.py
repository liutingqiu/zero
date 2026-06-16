# 零 · 多Agent协作层
from multi_agent.events import EventType, StreamPartition, CausalEvent, ShardedEventLog
from multi_agent.grounding import CodeAnalyzer, SideEffectDetector, validate_execution
from multi_agent.consensus import AdversarialResult, AdversarialConsensus
from multi_agent.contracts import AgentContract, ContractViolation, get_contract, enforce_event, enforce_output, CONTRACTS
from multi_agent.blackboard import BlackboardV5
from multi_agent.agents import ContractAgent, PlannerV5, ExecutorV5, CriticV5, SynthesizerV5
from multi_agent.orchestrator import MultiAgentOrchestratorV5, collaborate
from multi_agent.stability import (
    VectorClock, TemporalValidator, ConvergenceState, ConvergenceEngine,
    RepairPlan, RepairSystem,
)
from multi_agent.orchestrators import (
    SemanticConvergence, _reasoning_similarity,
    FailureTrace, GlobalReplanner, EventCompressor,
    MultiAgentOrchestratorV6, collaborate_v6,
    MultiAgentOrchestratorV7, collaborate_v7,
    MultiAgentOrchestratorV8, collaborate_v8,
    GroundTruth, GroundedJudge,
)
