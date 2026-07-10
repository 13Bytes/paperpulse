import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
import yaml

from api import agent as agent_module
from api import main as main_module
from api.agent import PaperpulseAgent
from api.arxiv_client import ArxivClient
from api.codex_agent import CodexCliAgent
from api.file_handler import FileHandler
from api.settings import AppSettings, build_arxiv_query, load_app_settings, load_config, parse_bool
from api.summary_backend import create_summary_backend
from api.utils import add_markdown_links, download_pdf
from api.webs import create_blogpost


class FakeResponse:
    def __init__(self, body: str | bytes):
        self.body = body.encode() if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


@pytest.fixture
def sample_paper():
    return {
        "title": "Test Paper Title",
        "authors": ["Author One", "Author Two"],
        "summary": "This is a test summary",
        "url": "https://arxiv.org/abs/2501.12345",
    }


@pytest.fixture
def sample_entry():
    xml_string = """
    <entry xmlns="http://www.w3.org/2005/Atom">
        <title>Test Paper Title</title>
        <author><name>Author One</name></author>
        <author><name>Author Two</name></author>
        <summary>This is a test summary</summary>
        <id>https://arxiv.org/abs/2501.12345</id>
        <updated>2025-01-17T12:00:00Z</updated>
    </entry>
    """
    return ET.fromstring(xml_string)


def test_load_config_from_explicit_path(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("blog:\n  post_title: Test title\n", encoding="utf-8")

    assert load_config(config_path)["blog"]["post_title"] == "Test title"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("categories_and_keywords", "%28cat:cs.AI+OR+cat:cs.SE%29+AND+%28all:agentic%29"),
        ("categories_only", "cat:cs.AI+OR+cat:cs.SE"),
        ("keywords_only", "all:agentic"),
    ],
)
def test_build_arxiv_query_modes(mode, expected):
    config = {
        "search": {
            "categories": ["cs.AI", "cs.SE"],
            "keywords": ["agentic"],
            "mode": mode,
        }
    }

    assert build_arxiv_query(config) == expected


def test_build_arxiv_query_quotes_multi_word_keywords():
    config = {"search": {"keywords": ["AI aided design"], "mode": "keywords_only"}}

    assert build_arxiv_query(config) == "all:%22AI+aided+design%22"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), (None, False)],
)
def test_parse_bool(value, expected):
    assert parse_bool(value) is expected


def test_load_app_settings_codex_backend(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_ENV", "prod")
    monkeypatch.setenv("LLM_BACKEND", "codex_cli")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_MODEL", "gpt-5")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "123")

    settings = load_app_settings()

    assert settings.project_dir == tmp_path
    assert settings.project_env == "prod"
    assert settings.llm_backend == "codex_cli"
    assert settings.codex_home == codex_home
    assert settings.codex_model == "gpt-5"
    assert settings.codex_timeout_seconds == 123


def test_load_app_settings_rejects_invalid_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "unknown")

    with pytest.raises(ValueError, match="LLM_BACKEND"):
        load_app_settings()


class TestArxivClient:
    def test_process_paper_entry(self, sample_entry):
        result = ArxivClient()._process_paper_entry(sample_entry)

        assert result == {
            "title": "Test Paper Title",
            "authors": ["Author One", "Author Two"],
            "summary": "This is a test summary",
            "url": "https://arxiv.org/abs/2501.12345",
        }

    def test_retrieve_daily_results_stops_at_cutoff(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Newest</title>
                <author><name>Ada Lovelace</name></author>
                <summary>Summary 1</summary>
                <id>https://arxiv.org/abs/2601.00001</id>
                <updated>2026-01-02T12:00:00Z</updated>
            </entry>
            <entry>
                <title>Still recent</title>
                <author><name>Grace Hopper</name></author>
                <summary>Summary 2</summary>
                <id>https://arxiv.org/abs/2601.00002</id>
                <updated>2026-01-01T13:00:00Z</updated>
            </entry>
            <entry>
                <title>Too old</title>
                <author><name>Katherine Johnson</name></author>
                <summary>Summary 3</summary>
                <id>https://arxiv.org/abs/2601.00003</id>
                <updated>2026-01-01T11:00:00Z</updated>
            </entry>
        </feed>
        """
        client = ArxivClient(urlopen=lambda _url: FakeResponse(feed), sleep=lambda _seconds: None)

        papers = client.retrieve_daily_results(
            now=datetime(2026, 1, 2, 12, tzinfo=UTC)
        )

        assert [paper["title"] for paper in papers] == ["Newest", "Still recent"]

    def test_retrieve_daily_results_returns_partial_results_after_retries(self):
        sleeps = []
        client = ArxivClient(
            urlopen=lambda _url: (_ for _ in ()).throw(URLError("offline")),
            sleep=sleeps.append,
        )

        assert client.retrieve_daily_results(max_retries=2) == []
        assert sleeps == [5]

    def test_retrieve_daily_results_handles_malformed_xml(self):
        client = ArxivClient(
            urlopen=lambda _url: FakeResponse("<feed>"),
            sleep=lambda _seconds: None,
        )

        assert client.retrieve_daily_results(max_retries=1) == []

    def test_get_pdf_url(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <link rel="related" title="pdf" href="https://arxiv.org/pdf/1234.5678" />
            </entry>
        </feed>
        """
        client = ArxivClient(urlopen=lambda _url: FakeResponse(feed))

        assert client.get_pdf_url("https://arxiv.org/abs/1234.5678") == "https://arxiv.org/pdf/1234.5678"

    def test_extract_and_filter_titles(self):
        client = ArxivClient()
        content = "1. **Title One** - Description\n2. **Title Two** - Another description"

        titles = client.extract_titles(content)
        result = client.filter_dicts_by_titles(
            [{"title": "Title One"}, {"title": "Title Three"}],
            titles,
        )

        assert titles == ["Title One", "Title Two"]
        assert result == [{"title": "Title One"}]


class FakeSDKAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs["name"]


class FakeModelSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeRunner:
    calls = []

    @staticmethod
    def run_sync(agent, text):
        FakeRunner.calls.append((agent.name, text))
        if agent.name == "Summary Combiner":
            return SimpleNamespace(final_output="combined summary")
        return SimpleNamespace(final_output=f"summary for {agent.name}")


@pytest.fixture
def fake_agents_sdk(monkeypatch):
    FakeRunner.calls = []
    monkeypatch.setattr(agent_module, "SDKAgent", FakeSDKAgent)
    monkeypatch.setattr(agent_module, "ModelSettings", FakeModelSettings)
    monkeypatch.setattr(agent_module, "Runner", FakeRunner)


class TestPaperpulseAgent:
    def test_format_and_batch_papers(self, fake_agents_sdk, sample_paper):
        llm_agent = PaperpulseAgent({}, model="test-model")

        assert llm_agent._format_paper(sample_paper) == (
            "**Title:** Test Paper Title\n"
            "**Authors:** Author One, Author Two\n"
            "**Summary:** This is a test summary\n"
        )
        assert llm_agent._batch_papers([sample_paper, sample_paper], max_chars=80) == [
            [sample_paper],
            [sample_paper],
        ]

    def test_identify_important_papers_uses_combiner_for_multiple_batches(
        self,
        fake_agents_sdk,
        monkeypatch,
        sample_paper,
    ):
        llm_agent = PaperpulseAgent({}, model="test-model")
        monkeypatch.setattr(
            llm_agent,
            "_batch_papers",
            lambda papers, max_chars: [[papers[0]], [papers[1]]],
        )

        summary = llm_agent.identify_important_papers([sample_paper, sample_paper])

        assert summary == "combined summary"
        assert [call[0] for call in FakeRunner.calls] == [
            "Engineering Research Summariser",
            "Engineering Research Summariser",
            "Summary Combiner",
        ]

    def test_identify_important_papers_rejects_empty_input(self, fake_agents_sdk):
        llm_agent = PaperpulseAgent({}, model="test-model")

        with pytest.raises(ValueError, match="No papers"):
            llm_agent.identify_important_papers([])


def make_codex_settings(tmp_path, **overrides):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    values = {
        "project_env": "dev",
        "project_dir": tmp_path,
        "openai_model": "test-model",
        "llm_backend": "codex_cli",
        "codex_home": codex_home,
        "codex_model": None,
        "codex_timeout_seconds": 900,
    }
    values.update(overrides)
    return AppSettings(**values)


class TestCodexCliAgent:
    def test_identify_important_papers_runs_codex_with_expected_args(
        self,
        tmp_path,
        sample_paper,
    ):
        calls = []
        settings = make_codex_settings(tmp_path, codex_model="gpt-5")

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps({"summary": "codex summary"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        llm_agent = CodexCliAgent({}, settings, run_command=fake_run)

        assert llm_agent.identify_important_papers([sample_paper]) == "codex summary"
        command, kwargs = calls[0]
        assert command[:3] == ["codex", "exec", "-"]
        assert "--sandbox" in command
        assert "read-only" in command
        assert "--ephemeral" in command
        assert "--skip-git-repo-check" in command
        assert "--output-schema" in command
        assert "--output-last-message" in command
        assert command[-2:] == ["--model", "gpt-5"]
        assert kwargs["input"].startswith("You are a research scientist.")
        assert "Test Paper Title" in kwargs["input"]
        assert kwargs["timeout"] == 900
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["CODEX_HOME"] == str(settings.codex_home)
        assert kwargs["shell"] is False

    def test_identify_important_papers_rejects_empty_input(self, tmp_path):
        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path))

        with pytest.raises(ValueError, match="No papers"):
            llm_agent.identify_important_papers([])

    def test_run_codex_fails_when_codex_home_is_missing(self, tmp_path):
        missing_home = tmp_path / "missing"
        settings = make_codex_settings(tmp_path, codex_home=missing_home)
        llm_agent = CodexCliAgent({}, settings)

        with pytest.raises(RuntimeError, match="CODEX_HOME does not exist"):
            llm_agent._run_codex("prompt")

    def test_run_codex_handles_nonzero_exit(self, tmp_path):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="login required")

        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path), run_command=fake_run)

        with pytest.raises(RuntimeError, match="Codex CLI failed"):
            llm_agent._run_codex("prompt")

    def test_run_codex_handles_timeout(self, tmp_path):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path), run_command=fake_run)

        with pytest.raises(RuntimeError, match="timed out"):
            llm_agent._run_codex("prompt")

    @pytest.mark.parametrize("payload", ["not json", "{}", '{"summary": ""}'])
    def test_run_codex_rejects_bad_json_output(self, tmp_path, payload):
        def fake_run(command, **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(payload, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path), run_command=fake_run)

        with pytest.raises(RuntimeError):
            llm_agent._run_codex("prompt")


def test_create_summary_backend_selects_openai_api(fake_agents_sdk, tmp_path):
    settings = AppSettings(
        project_env="dev",
        project_dir=tmp_path,
        openai_model="test-model",
        llm_backend="openai_api",
    )

    assert isinstance(create_summary_backend({}, settings), PaperpulseAgent)


def test_create_summary_backend_selects_codex_cli(tmp_path):
    settings = make_codex_settings(tmp_path)

    assert isinstance(create_summary_backend({}, settings), CodexCliAgent)


def test_main_uses_selected_summary_backend(monkeypatch, tmp_path, sample_paper):
    class FakeArxivClient:
        def __init__(self, *_args):
            pass

        def retrieve_daily_results(self):
            return [sample_paper]

    class FakeBackend:
        def identify_important_papers(self, papers):
            assert papers == [sample_paper]
            return "Summary with Test Paper Title"

    created_posts = []
    settings = AppSettings(
        project_env="prod",
        project_dir=tmp_path,
        openai_model="test-model",
        llm_backend="codex_cli",
    )

    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "load_app_settings", lambda: settings)
    monkeypatch.setattr(main_module, "load_config", lambda: {})
    monkeypatch.setattr(main_module, "ArxivClient", FakeArxivClient)
    monkeypatch.setattr(
        main_module,
        "create_summary_backend",
        lambda config, app_settings: FakeBackend(),
    )
    monkeypatch.setattr(
        main_module,
        "create_blogpost",
        lambda summary, num_papers, config, settings: (
            created_posts.append((summary, num_papers, config, settings))
            or tmp_path / "post.md"
        ),
    )

    result = main_module.main()

    assert result.ok is True
    assert created_posts[0][1] == 1
    assert created_posts[0][3] is settings


class TestFileHandler:
    def test_save_and_load_papers_under_data_dir(self, tmp_path, sample_paper):
        data_dir = tmp_path / "data"
        file_handler = FileHandler(data_dir)
        test_date = datetime(2025, 1, 1)

        file_handler.save_papers([sample_paper], date=test_date)

        assert (data_dir / "papers-2025-01-01.pkl").exists()
        assert file_handler.load_papers(date=test_date) == [sample_paper]


def test_add_markdown_links_links_titles_and_author_citations():
    text = "Read Paper One and Smith et al. (2025)."
    paper_list = [
        {
            "title": "Paper One",
            "authors": ["Alex Smith", "Taylor Jones"],
            "url": "http://example.com/2501.00001",
            "summary": "Summary",
        }
    ]

    result = add_markdown_links(text, paper_list)

    assert '<a href="http://example.com/2501.00001" target="_blank">Paper One</a>' in result
    assert (
        '<a href="http://example.com/2501.00001" target="_blank">Smith et al. (2025)</a>'
        in result
    )


def test_download_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr("urllib.request.urlopen", lambda _url: FakeResponse(b"PDF content"))
    monkeypatch.setattr("api.utils.tempfile.gettempdir", lambda: str(tmp_path))

    expected_path = tmp_path / "test.pdf"
    assert download_pdf("http://example.com/paper.pdf", "test.pdf") == str(expected_path)
    assert expected_path.read_bytes() == b"PDF content"


def test_create_blogpost_creates_directory_and_safe_front_matter(tmp_path):
    settings = AppSettings(
        project_env="dev",
        project_dir=tmp_path,
        openai_model="test-model",
    )
    config = {"blog": {"post_title": "Daily: Research Summary"}}

    post_path = create_blogpost(
        "Test summary content",
        5,
        config=config,
        settings=settings,
        date=datetime(2025, 1, 1),
    )

    content = post_path.read_text(encoding="utf-8")
    front_matter = yaml.safe_load(content.split("---", 2)[1])

    assert post_path == tmp_path / "blog" / "_posts" / "2025-01-01-daily-summary.markdown"
    assert front_matter["title"] == "Daily: Research Summary"
    assert front_matter["num_papers"] == 5
    assert "Test summary content" in content


def test_jekyll_branding_comes_only_from_central_config():
    project_root = Path(__file__).parents[2]
    config_text = (project_root / "blog" / "_config.yml").read_text(encoding="utf-8")
    # BaseLoader tolerates Jekyll's custom !ENV tag; scalar types are irrelevant here.
    jekyll_config = yaml.load(config_text, Loader=yaml.BaseLoader)

    assert not {"title", "tagline", "description"} & jekyll_config.keys()
    assert (project_root / "blog" / "_plugins" / "paperpulse_config.rb").is_file()

    expected_mount = "./config.yaml:/srv/jekyll/_data/paperpulse.yml:ro"
    for compose_name in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((project_root / compose_name).read_text(encoding="utf-8"))
        assert expected_mount in compose["services"]["blog"]["volumes"]
