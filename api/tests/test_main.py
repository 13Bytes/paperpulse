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
from api.auth import send_magic_link
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
    monkeypatch.setenv("SESSION_SECRET", "test-production-secret")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)

    settings = load_app_settings()

    assert settings.project_dir == tmp_path
    assert settings.project_env == "prod"
    assert settings.llm_backend == "codex_cli"
    assert settings.codex_home == codex_home
    assert settings.codex_model == "gpt-5"
    assert settings.codex_timeout_seconds == 123
    assert settings.session_cookie_secure is True


def test_load_app_settings_allows_explicit_insecure_cookie_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_ENV", "prod")
    monkeypatch.setenv("SESSION_SECRET", "test-production-secret")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    assert load_app_settings().session_cookie_secure is False


def test_load_app_settings_rejects_invalid_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "unknown")

    with pytest.raises(ValueError, match="LLM_BACKEND"):
        load_app_settings()


def test_send_magic_link_uses_implicit_tls_on_port_465(monkeypatch, tmp_path):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            calls.append(("connect", host, port, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, **_kwargs):
            calls.append(("starttls",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr("api.auth.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(
        "api.auth.smtplib.SMTP",
        lambda *_args, **_kwargs: pytest.fail("Port 465 must use implicit TLS"),
    )
    settings = AppSettings(
        project_env="test",
        project_dir=tmp_path,
        openai_model="unused",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="sender@example.com",
        smtp_password="secret",
        smtp_starttls=True,
    )

    send_magic_link(settings, "reader@example.com", "token")

    assert calls[0][0:3] == ("connect", "smtp.example.com", 465)
    assert "context" in calls[0][3]
    assert ("starttls",) not in calls
    assert calls[-1] == ("send", "reader@example.com")


def test_send_magic_link_wraps_smtp_failures(monkeypatch, tmp_path):
    def fail_connect(*_args, **_kwargs):
        raise TimeoutError("SMTP timed out")

    monkeypatch.setattr("api.auth.smtplib.SMTP", fail_connect)
    settings = AppSettings(
        project_env="test",
        project_dir=tmp_path,
        openai_model="unused",
        smtp_host="smtp.example.com",
    )

    with pytest.raises(RuntimeError, match="couldn't send"):
        send_magic_link(settings, "reader@example.com", "token")


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
                <updated>2026-01-02T11:59:59Z</updated>
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

        papers = client.retrieve_daily_results(now=datetime(2026, 1, 2, 12, tzinfo=UTC))

        assert [paper["title"] for paper in papers] == ["Newest", "Still recent"]

    def test_retrieve_daily_results_uses_explicit_half_open_window(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>After window</title><author><name>A</name></author>
              <summary>Later</summary><id>https://arxiv.org/abs/1</id>
              <updated>2026-01-02T07:00:00Z</updated></entry>
            <entry><title>At window end</title><author><name>B</name></author>
              <summary>Boundary</summary><id>https://arxiv.org/abs/2</id>
              <updated>2026-01-02T06:00:00Z</updated></entry>
            <entry><title>Inside window</title><author><name>C</name></author>
              <summary>Inside</summary><id>https://arxiv.org/abs/3</id>
              <updated>2026-01-02T05:59:59Z</updated></entry>
            <entry><title>At window start</title><author><name>D</name></author>
              <summary>Boundary</summary><id>https://arxiv.org/abs/4</id>
              <updated>2026-01-01T06:00:00Z</updated></entry>
            <entry><title>Before window</title><author><name>E</name></author>
              <summary>Earlier</summary><id>https://arxiv.org/abs/5</id>
              <updated>2026-01-01T05:59:59Z</updated></entry>
        </feed>
        """
        client = ArxivClient(urlopen=lambda _url: FakeResponse(feed), sleep=lambda _seconds: None)

        papers = client.retrieve_daily_results(
            window_start=datetime(2026, 1, 1, 6, tzinfo=UTC),
            window_end=datetime(2026, 1, 2, 6, tzinfo=UTC),
        )

        assert [paper["title"] for paper in papers] == ["Inside window", "At window start"]

    def test_retrieve_daily_results_returns_partial_results_after_retries(self):
        sleeps = []
        client = ArxivClient(
            urlopen=lambda _url: (_ for _ in ()).throw(URLError("offline")),
            sleep=sleeps.append,
        )

        assert client.retrieve_daily_results(max_retries=2) == []
        assert sleeps == [5]

    def test_retrieve_daily_results_treats_empty_feed_as_a_successful_no_op(self):
        calls = []
        sleeps = []

        def empty_feed(url):
            calls.append(url)
            return FakeResponse('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')

        client = ArxivClient(urlopen=empty_feed, sleep=sleeps.append)

        assert client.retrieve_daily_results() == []
        assert len(calls) == 1
        assert calls[0].startswith("https://export.arxiv.org/")
        assert sleeps == []

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

        assert (
            client.get_pdf_url("https://arxiv.org/abs/1234.5678")
            == "https://arxiv.org/pdf/1234.5678"
        )

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
            "Interdisciplinary Research Summariser",
            "Interdisciplinary Research Summariser",
            "Summary Combiner",
        ]

    def test_identify_important_papers_rejects_empty_input(self, fake_agents_sdk):
        llm_agent = PaperpulseAgent({}, model="test-model")

        with pytest.raises(ValueError, match="No papers"):
            llm_agent.identify_important_papers([])

    def test_summarize_weekly_rejects_empty_input(self, fake_agents_sdk):
        llm_agent = PaperpulseAgent({}, model="test-model")

        with pytest.raises(ValueError, match="No daily reports"):
            llm_agent.summarize_weekly([])

    def test_summarize_weekly_joins_daily_reports(self, fake_agents_sdk):
        llm_agent = PaperpulseAgent({}, model="test-model")

        result = llm_agent.summarize_weekly(["Monday report", "Tuesday report"])

        assert result == "summary for Weekly Research Summariser"
        assert FakeRunner.calls == [
            (
                "Weekly Research Summariser",
                "Monday report\n\n--- DAILY REPORT ---\n\nTuesday report",
            )
        ]


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

    def test_summarize_weekly_rejects_empty_input(self, tmp_path):
        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path))

        with pytest.raises(ValueError, match="No daily reports"):
            llm_agent.summarize_weekly([])

    def test_summarize_weekly_builds_joined_prompt(self, monkeypatch, tmp_path):
        llm_agent = CodexCliAgent({}, make_codex_settings(tmp_path))
        prompts = []
        monkeypatch.setattr(
            llm_agent,
            "_run_codex",
            lambda prompt: prompts.append(prompt) or "weekly summary",
        )

        result = llm_agent.summarize_weekly(["Monday report", "Tuesday report"])

        assert result == "weekly summary"
        assert "Create a coherent weekly report" in prompts[0]
        assert prompts[0].endswith(
            "Monday report\n\n--- DAILY REPORT ---\n\nTuesday report"
        )

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


def test_main_runs_multi_topic_daily_job(monkeypatch):
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        main_module,
        "run_daily",
        lambda: SimpleNamespace(succeeded=2, skipped=1, failed=0),
    )

    result = main_module.main()

    assert result.ok is True
    assert result.message == "Daily topic run complete: 2 published, 1 skipped, 0 failed"


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
        '<a href="http://example.com/2501.00001" target="_blank">Smith et al. (2025)</a>' in result
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


def test_compose_uses_web_scheduler_and_sqlite_data_volume():
    project_root = Path(__file__).parents[2]
    for compose_name in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((project_root / compose_name).read_text(encoding="utf-8"))
        assert {"migrate", "web", "scheduler"} <= compose["services"].keys()
        assert "blog" not in compose["services"]
        assert "./data:/app/data" in compose["services"]["web"]["volumes"]
