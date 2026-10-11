"""
Тесты для speed_test.py.

Сетевые вызовы мокаются через unittest.mock: тесты не зависят от интернета
и работают детерминированно.

Запуск: pytest
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import speed_test


def make_response(chunks: list[bytes], status_code: int = 200) -> MagicMock:
    """
    Собирает мок requests.Response:
      - работает как контекстный менеджер (with ... as r);
      - iter_content отдаёт переданные чанки;
      - raise_for_status бросает HTTPError при статусе >= 400.
    """
    response = MagicMock()
    response.status_code = status_code
    response.iter_content.return_value = iter(chunks)
    response.__enter__.return_value = response
    response.__exit__.return_value = False

    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} Client Error"
        )
    else:
        response.raise_for_status.return_value = None

    return response


def make_session(response: MagicMock) -> MagicMock:
    """
    Мок requests.Session, у которого session.get(...) возвращает переданный
    response как контекстный менеджер.
    """
    session = MagicMock()
    session.get.return_value = response
    return session


@pytest.fixture(autouse=True)
def _reset_logging():
    """Сбрасываем хендлеры и уровень логгера между тестами."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


# ---------- measure ----------

def test_measure_returns_time_and_size():
    """measure возвращает время и суммарный размер скачанных данных."""
    chunks = [b"a" * 1000, b"b" * 2000, b""]
    session = make_session(make_response(chunks))

    elapsed, size = speed_test.measure(session, "https://example.com/file", timeout=10)

    assert size == 3000
    assert elapsed >= 0


def test_measure_reads_in_chunks():
    """measure читает ответ чанками указанного размера."""
    chunks = [b"x" * 10, b"y" * 10, b""]
    response = make_response(chunks)
    session = make_session(response)

    speed_test.measure(session, "https://example.com/file", timeout=10)

    response.iter_content.assert_called_once_with(chunk_size=speed_test.CHUNK_SIZE)


def test_measure_propagates_http_error():
    """HTTP-ошибка прокидывается наверх (обрабатывается в main)."""
    session = make_session(make_response([], status_code=404))

    with pytest.raises(requests.HTTPError):
        speed_test.measure(session, "https://example.com/not-found", timeout=10)


def test_measure_logs_debug(caplog):
    """measure пишет размер и время на уровне DEBUG."""
    session = make_session(make_response([b"x" * 100, b""]))

    with caplog.at_level(logging.DEBUG, logger="speed_test"):
        speed_test.measure(session, "https://example.com/file", timeout=10)

    assert any("Запрос завершён" in record.message for record in caplog.records)


# ---------- warm_up ----------

def test_warm_up_calls_session_get():
    """Прогрев делает один запрос к URL."""
    session = make_session(make_response([b"warm", b""]))

    speed_test.warm_up(session, "https://example.com/file", timeout=10)

    session.get.assert_called_once()
    assert session.get.call_args.args[0] == "https://example.com/file"


def test_warm_up_swallows_request_exception(caplog):
    """Ошибка прогрева не должна ронять программу, но пишется в WARNING."""
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")

    with caplog.at_level(logging.WARNING, logger="speed_test"):
        speed_test.warm_up(session, "https://example.com/file", timeout=10)

    assert any(
        record.levelno == logging.WARNING and "Прогрев" in record.message
        for record in caplog.records
    )


# ---------- human_size ----------

@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0.00 Б"),
        (512, "512.00 Б"),
        (1024, "1.00 КБ"),
        (1024 * 1024, "1.00 МБ"),
        (1024 * 1024 * 1024, "1.00 ГБ"),
    ],
)
def test_human_size(num_bytes: int, expected: str):
    assert speed_test.human_size(num_bytes) == expected


# ---------- parse_args ----------

def test_parse_args_defaults():
    args = speed_test.parse_args(["https://example.com/file"])
    assert args.url == "https://example.com/file"
    assert args.requests == speed_test.DEFAULT_REQUESTS
    assert args.timeout == speed_test.DEFAULT_TIMEOUT
    assert args.no_warmup is False
    assert args.verbose is False
    assert args.quiet is False
    assert args.log_file is None


def test_parse_args_overrides():
    args = speed_test.parse_args(
        ["https://example.com/file", "-n", "3", "-t", "5", "--no-warmup", "-v"]
    )
    assert args.requests == 3
    assert args.timeout == 5
    assert args.no_warmup is True
    assert args.verbose is True


# ---------- setup_logging ----------

def test_setup_logging_verbose_sets_debug(capsys):
    speed_test.setup_logging(verbose=True)
    logging.getLogger().debug("dbg")
    captured = capsys.readouterr()
    assert "dbg" in captured.out


def test_setup_logging_quiet_hides_info(capsys):
    speed_test.setup_logging(quiet=True)
    logging.getLogger().info("hidden")
    logging.getLogger().warning("shown")
    captured = capsys.readouterr()
    assert "hidden" not in captured.out
    assert "shown" in captured.err


def test_setup_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "run.log"
    speed_test.setup_logging(log_file=str(log_file))
    logging.getLogger().info("hello-file")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello-file" in log_file.read_text(encoding="utf-8")


# ---------- main ----------

def _patch_session(session_mock: MagicMock):
    """Патчит requests.Session, чтобы main использовал наш мок."""

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.get = session_mock.get

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return patch.object(speed_test.requests, "Session", FakeSession)


def test_main_logs_speed_and_returns_zero(capsys):
    """Успешный прогон: returncode 0 и в выводе есть 'МБ/с'."""
    chunks = [b"a" * 1024, b"b" * 1024, b""]  # 2 КБ на запрос
    session = make_session(make_response(chunks))

    with _patch_session(session):
        exit_code = speed_test.main(
            ["https://example.com/file", "-n", "2", "-t", "5", "--no-warmup"]
        )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ИТОГИ ЗАМЕРА" in captured.out
    assert "МБ/с" in captured.out


def test_main_returns_error_when_all_requests_fail(capsys):
    """Если все запросы упали — returncode 1 и понятное сообщение."""
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("no network")

    with _patch_session(session):
        exit_code = speed_test.main(
            ["https://example.com/file", "-n", "2", "-t", "5", "--no-warmup"]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Не удалось выполнить ни одного запроса" in captured.err


def test_main_rejects_zero_requests(capsys):
    """n < 1 — ошибка валидации, returncode 2."""
    exit_code = speed_test.main(["https://example.com/file", "-n", "0"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "должно быть >= 1" in captured.err.lower() or ">= 1" in captured.err