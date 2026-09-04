import pytest
from loguru import logger


@pytest.fixture(scope="session", autouse=True)
def isolated_test_log_sink(tmp_path_factory):
    """Keep test events in pytest-owned storage, never operational log files."""
    logger.remove()
    sink_id = logger.add(
        tmp_path_factory.mktemp("logs") / "test.log",
        level="DEBUG",
        backtrace=False,
        diagnose=False,
    )
    yield
    logger.remove(sink_id)
