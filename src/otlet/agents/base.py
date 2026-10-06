from abc import ABC, abstractmethod


class AgentBase(ABC):
    """Base class for all agents in the literature management system."""

    name: str = "base"

    def __init__(self, name: str | None = None) -> None:
        if name is not None:
            self.name = name

    @abstractmethod
    def run(self, *args, **kwargs):
        """Execute the agent's primary task."""
        ...
