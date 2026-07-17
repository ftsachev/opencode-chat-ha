"""Conftest for opencode-chat-ha tests — mocks homeassistant."""
import sys
from unittest.mock import MagicMock

# Mock homeassistant and its submodules before any import
ha_mock = MagicMock()
ha_mock.__path__ = []

# Mock homeassistant.components as a package (not a MagicMock)
components_mock = MagicMock()
components_mock.__path__ = []

sys.modules["homeassistant"] = ha_mock
sys.modules["homeassistant.components"] = components_mock
sys.modules["homeassistant.components.frontend"] = MagicMock()
sys.modules["homeassistant.components.http"] = MagicMock()
sys.modules["homeassistant.components.websocket_api"] = MagicMock()
sys.modules["homeassistant.components.lovelace"] = MagicMock()
sys.modules["homeassistant.components.lovelace.const"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["voluptuous"] = MagicMock()
