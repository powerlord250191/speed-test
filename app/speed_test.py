"""
Скрипт для замера скорости интернета.

Принимает URL тяжёлого ресурса (например, большой картинки), прогревает
соединение, затем последовательно выполняет N запросов через одну
requests.Session (keep-alive), замеряет время каждого, считает суммарный
объём скачанных данных и выводит среднюю скорость в МБ/с.

Зависимости: requests (см. requirements.txt).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from statistics import mean

import requests

# Размер чанка при чтении ответа. 64 КБ — разумный баланс между
# количеством системных вызовов и потреблением памяти.
CHUNK_SIZE = 64 * 1024

# Значения по умолчанию.
DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30

# User-Agent, чтобы CDN не резали запросы как «подозрительные».
USER_AGENT = "Mozilla/5.0 (compatible; SpeedTest/1.0)"

logger = logging.getLogger("speed_test")


def measure(session: requests.Session, url: str, timeout: int) -> tuple[float, int]:
    """
    Выполняет один GET-запрос и возвращает (время в секундах, размер в байтах).

    Время замеряется вокруг чтения тела ответа: соединение уже установлено
    на прогреве, поэтому метрика отражает скорость передачи данных.

    Тело читается потоково (stream=True) чанками, чтобы не держать в памяти
    большие файлы и чтобы замер отражал реальную скорость скачивания.
    """
    start = time.perf_counter()
    total_bytes = 0

    with session.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            total_bytes += len(chunk)

    elapsed = time.perf_counter() - start
    logger.debug("Запрос завершён: %.3f с, %d байт", elapsed, total_bytes)
    return elapsed, total_bytes


def warm_up(session: requests.Session, url: str, timeout: int) -> None:
    """
    Холостой запрос для прогрева соединения.

    Первый запрос включает DNS-резолв и TLS-handshake, поэтому он обычно
    заметно медленнее остальных. Делаем один «холостой» запрос заранее,
    чтобы он не попал в статистику.

    Прогрев — best-effort: если он упал, основная часть попробует выполниться
    и вернуть более явную ошибку.
    """
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            # Читаем только начало тела — этого достаточно для прогрева.
            next(response.iter_content(chunk_size=CHUNK_SIZE), b"")
    except requests.RequestException as exc:
        logger.warning("Прогрев соединения не удался (%s): %s", type(exc).__name__, exc)
        return

    logger.debug("Прогрев соединения завершён")


def human_size(num_bytes: int) -> str:
    """Человекочитаемый размер (Б, КБ, МБ, ГБ)."""
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} ТБ"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Замер скорости интернета: N последовательных запросов к URL.",
    )
    parser.add_argument(
        "url",
        help="URL тяжёлого ресурса (например, большая картинка).",
    )
    parser.add_argument(
        "-n", "--requests",
        type=int,
        default=DEFAULT_REQUESTS,
        help=f"Количество запросов (по умолчанию {DEFAULT_REQUESTS}).",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Таймаут одного запроса в секундах (по умолчанию {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="Отключить прогрев соединения (для отладки и сравнения).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод (уровень DEBUG).",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Тихий режим: только итоговая сводка (уровень WARNING).",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Путь к файлу для логов (по умолчанию — только stdout).",
    )
    return parser.parse_args(argv)


def setup_logging(
    verbose: bool = False,
    quiet: bool = False,
    log_file: str | None = None,
) -> None:
    """
    Настраивает корневой логгер.

    - INFO идёт в stdout (основной поток вывода CLI);
    - WARNING+ дополнительно идёт в stderr (чтобы ошибки были видны при
      перенаправлении stdout);
    - при --log-file все сообщения пишутся и в файл.
    """
    if verbose and quiet:
        # Взаимоисключающие флаги: тихо побеждает, но сообщим об этом.
        verbose = False

    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Чистим прежние хендлеры (важно при повторных вызовах в тестах).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(fmt)
    stdout_handler.addFilter(lambda record: record.levelno < logging.WARNING)
    root.addHandler(stdout_handler)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(max(level, logging.WARNING))
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file)

    if args.requests < 1:
        logger.critical("Количество запросов должно быть >= 1")
        return 2

    logger.info("Замер скорости для: %s", args.url)
    logger.info("Запросов: %d, таймаут: %d с", args.requests, args.timeout)

    headers = {"User-Agent": USER_AGENT}
    times: list[float] = []
    total_bytes = 0

    # Одна сессия на весь прогон — keep-alive, соединение переиспользуется.
    with requests.Session() as session:
        session.headers.update(headers)

        if not args.no_warmup:
            logger.info("Прогрев соединения ...")
            warm_up(session, args.url, args.timeout)

        for i in range(1, args.requests + 1):
            try:
                elapsed, size = measure(session, args.url, args.timeout)
            except requests.RequestException as exc:
                logger.error(
                    "[%d/%d] ОШИБКА | %s: %s",
                    i, args.requests, type(exc).__name__, exc,
                )
                continue

            times.append(elapsed)
            total_bytes += size
            speed = size / elapsed / (1024 * 1024) if elapsed else 0.0
            logger.info(
                "[%d/%d] OK | %.3f с | %s | %.2f МБ/с",
                i, args.requests, elapsed, human_size(size), speed,
            )

    if not times:
        logger.error("Не удалось выполнить ни одного запроса. Проверьте URL и соединение.")
        return 1

    total_time = sum(times)
    avg_time = mean(times)
    avg_speed = total_bytes / total_time / (1024 * 1024) if total_time else 0.0

    logger.info("=" * 60)
    logger.info("ИТОГИ ЗАМЕРА")
    logger.info("=" * 60)
    logger.info("Всего запросов:        %d", args.requests)
    logger.info("Успешных:              %d", len(times))
    logger.info("Скачано всего:         %s", human_size(total_bytes))
    logger.info("Среднее время запроса: %.3f с", avg_time)
    logger.info("Средняя скорость:      %.2f МБ/с", avg_speed)
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(["https://disk.yandex.ru/i/8DnyUWfGggE6XA"]))