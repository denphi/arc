import logging
from typing import Any

logger = logging.getLogger(__name__)


class ComponentRegistry:
    """Registry for agents, skills, adapters, extensions, and providers."""

    def __init__(self):
        self._agents: dict[str, Any] = {}
        self._skills: dict[str, Any] = {}
        self._adapters: dict[str, Any] = {}
        self._extensions: dict[str, Any] = {}
        self._providers: dict[str, Any] = {}
        self._workflows: dict[str, dict] = {}
        self._evaluators: dict[str, Any] = {}
        self._prompts: dict[str, Any] = {}
        self._templates: dict[str, Any] = {}
        self._constraints: dict[str, Any] = {}
        self._vocabularies: dict[str, Any] = {}

    # --- agents ---

    def register_agent(self, name: str, agent_class: Any) -> None:
        self._agents[name] = agent_class
        logger.info("Agent registered: %s", name)

    def get_agent(self, name: str) -> Any:
        if name not in self._agents:
            raise KeyError(f"Agent not found: {name}")
        return self._agents[name]

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    # --- skills ---

    def register_skill(self, name: str, skill: Any) -> None:
        self._skills[name] = skill
        logger.info("Skill registered: %s", name)

    def get_skill(self, name: str) -> Any:
        if name not in self._skills:
            raise KeyError(f"Skill not found: {name}")
        return self._skills[name]

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())

    # --- adapters ---

    def register_adapter(self, name: str, adapter: Any) -> None:
        self._adapters[name] = adapter
        logger.info("Adapter registered: %s", name)

    def get_adapter(self, name: str) -> Any:
        if name not in self._adapters:
            raise KeyError(f"Adapter not found: {name}")
        return self._adapters[name]

    def list_adapters(self) -> list[str]:
        return list(self._adapters.keys())

    # --- extensions ---

    def register_extension(self, name: str, extension: Any) -> None:
        self._extensions[name] = extension
        logger.info("Extension registered: %s", name)

    def get_extension(self, name: str) -> Any:
        return self._extensions.get(name)

    # --- providers ---

    def register_provider(self, name: str, provider: Any) -> None:
        self._providers[name] = provider
        logger.info("Provider registered: %s", name)

    def get_provider(self, name: str) -> Any:
        if name not in self._providers:
            raise KeyError(f"Provider not found: {name}")
        return self._providers[name]

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    # --- workflows ---

    def register_workflow(self, name: str, definition: dict) -> None:
        self._workflows[name] = definition
        logger.info("Workflow registered: %s", name)

    def get_workflow(self, name: str) -> dict:
        if name not in self._workflows:
            raise KeyError(f"Workflow not found: {name}")
        return self._workflows[name]

    def list_workflows(self) -> list[str]:
        return list(self._workflows.keys())

    # --- package resources ---

    def register_evaluator(self, name: str, evaluator: Any) -> None:
        self._evaluators[name] = evaluator
        logger.info("Evaluator registered: %s", name)

    def get_evaluator(self, name: str) -> Any:
        if name not in self._evaluators:
            raise KeyError(f"Evaluator not found: {name}")
        return self._evaluators[name]

    def list_evaluators(self) -> list[str]:
        return list(self._evaluators.keys())

    def register_prompt(self, name: str, prompt: Any) -> None:
        self._prompts[name] = prompt
        logger.info("Prompt registered: %s", name)

    def get_prompt(self, name: str) -> Any:
        if name not in self._prompts:
            raise KeyError(f"Prompt not found: {name}")
        return self._prompts[name]

    def list_prompts(self) -> list[str]:
        return list(self._prompts.keys())

    def register_template(self, name: str, template: Any) -> None:
        self._templates[name] = template
        logger.info("Template registered: %s", name)

    def get_template(self, name: str) -> Any:
        if name not in self._templates:
            raise KeyError(f"Template not found: {name}")
        return self._templates[name]

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def register_constraint(self, name: str, constraint: Any) -> None:
        self._constraints[name] = constraint
        logger.info("Constraint registered: %s", name)

    def get_constraint(self, name: str) -> Any:
        if name not in self._constraints:
            raise KeyError(f"Constraint not found: {name}")
        return self._constraints[name]

    def list_constraints(self) -> list[str]:
        return list(self._constraints.keys())

    def register_vocabulary(self, name: str, vocabulary: Any) -> None:
        self._vocabularies[name] = vocabulary
        logger.info("Vocabulary registered: %s", name)

    def get_vocabulary(self, name: str) -> Any:
        if name not in self._vocabularies:
            raise KeyError(f"Vocabulary not found: {name}")
        return self._vocabularies[name]

    def list_vocabularies(self) -> list[str]:
        return list(self._vocabularies.keys())
