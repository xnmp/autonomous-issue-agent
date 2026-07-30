from issue_agent.adapters.github import GitHubGateway
from issue_agent.domain.ports import GitHubReads
from tests.fakes.in_memory_github import InMemoryGitHub


def test_real_gateway_and_shared_fake_satisfy_read_contract() -> None:
    assert isinstance(GitHubGateway("owner/repo"), GitHubReads)
    assert isinstance(InMemoryGitHub(), GitHubReads)
