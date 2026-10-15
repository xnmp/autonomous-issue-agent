from issue_agent.adapters.github import Accepted


class InMemoryGitHub:
    """Write-recording gateway for loop and composition tests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.labels: dict[tuple[str, int], set[str]] = {}

    def relabel(self, kind: str, number: int, remove: str | None, add: str | None) -> None:
        self.calls.append(("relabel", kind, number, remove, add))
        labels = self.labels.setdefault((kind, number), set())
        labels.discard(remove or "")
        labels.update({add} if add else set())

    def has_label(self, kind: str, number: int, label: str) -> bool:
        return label in self.labels.get((kind, number), set())

    def comment(self, kind: str, number: int, body: str) -> Accepted:
        self.calls.append(("comment", kind, number, body))
        return Accepted(len(self.calls))
