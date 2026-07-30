"""Print the operator configuration template from HarnessConfig metadata."""

from issue_agent.app.config import generated_env_reference


def main() -> None:
    print(generated_env_reference(), end="")


if __name__ == "__main__":
    main()
