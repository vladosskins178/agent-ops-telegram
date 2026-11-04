from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_ops_telegram.config import Config, load_config
from agent_ops_telegram.relay import Relay
from agent_ops_telegram.telegram import TelegramAPI
from agent_ops_telegram.tmux import Tmux

from .fakes import TOKEN, FakeTelegram, FakeTmux


@pytest.fixture(autouse=True, scope="session")
def _dev_null_untouched() -> Iterator[None]:
    """Guard: nothing in the suite may ever replace /dev/null."""
    import stat as st_

    yield
    assert st_.S_ISCHR(Path("/dev/null").stat().st_mode), "/dev/null is no longer a character device"


@pytest.fixture
def tg() -> Iterator[FakeTelegram]:
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def ftmux(tmp_path: Path) -> FakeTmux:
    d = tmp_path / "bin"
    d.mkdir()
    fake = FakeTmux(d)
    return fake


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """Never touch the real environment: no AOT_* variables, HOME in tmp, fake tmux state."""
    for var in ("AOT_CONFIG", "AOT_BOT", "AOT_TOKEN_AGENT", "AOT_TOKEN_SECOND"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("FAKE_TMUX_STATE", str(tmp_path / "bin" / "tmux-state.json"))
    # systemctl is never called for real in unit tests
    from agent_ops_telegram import services

    monkeypatch.setattr(services, "systemctl_available", lambda: False)


ConfigFactory = Callable[..., Config]


@pytest.fixture
def make_config(tmp_path: Path, tg: FakeTelegram, ftmux: FakeTmux) -> ConfigFactory:
    def factory(bot_extra: str = "", defaults_extra: str = "", *, token: str = TOKEN, name: str = "agent", mode: int = 0o600) -> Config:
        env = tmp_path / "secrets.env"
        env.write_text(f"AOT_TOKEN_{name.upper()}={token}\n")
        env.chmod(0o600)
        cfg = tmp_path / "config.toml"
        own = bot_extra.split("[bots.")[0]  # settings for this bot (other bots may follow)
        base = {"allowed_chats": "[42]", "tmux_target": '"agent"', "tmux_bin": f'"{ftmux.bin}"', "submit_delay": "0", "start_grace": "0"}
        lines = [f"{k} = {v}" for k, v in base.items() if f"{k} =" not in own]
        cfg.write_text(
            "[defaults]\n"
            f'env_file = "{env}"\n'
            f'state_dir = "{tmp_path / "state"}"\n'
            f'api_base = "{tg.base_url}"\n'
            'systemd = "none"\n'
            f"{defaults_extra}\n"
            f"[bots.{name}]\n" + "\n".join(lines) + f"\n{bot_extra}\n"
        )
        cfg.chmod(mode)
        return load_config(cfg)

    return factory


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept: list[float] = []
        self._skew = -1_799_000_000.0  # monotonic() = now + skew

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now + self._skew

    def step(self, seconds: float) -> None:
        """Step the wall clock only (an NTP correction); the monotonic clock does not move."""
        self.now += seconds
        self._skew -= seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_relay(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> Callable[..., Relay]:
    def factory(bot_extra: str = "", **kwargs: Any) -> Relay:
        cfg = make_config(bot_extra, **kwargs)
        bot = cfg.bot(None)
        api = TelegramAPI(bot.token(), bot.api_base, sleep=clock.sleep, retries=2)
        tmux = Tmux(bot.tmux_target, binary=bot.tmux_bin, sleep=lambda s: None)
        logger = logging.getLogger("aot.test")
        return Relay(bot, api, tmux, logger=logger, clock=clock, sleep=clock.sleep, config_path=cfg.path, monotonic=clock.monotonic)

    return factory
