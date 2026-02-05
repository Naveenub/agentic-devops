from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class AgentState:
    query: str
    logs: Optional[str] = None
    diagnosis: Optional[str] = None
    suggestion: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    confidence: float = 0.0
