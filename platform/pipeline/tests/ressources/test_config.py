"""Unit tests targeting platform settings parsing logic and resource caching wrappers."""

from unittest.mock import MagicMock, patch

from galadril_pipeline.resources.config import VisionConfigResource


@patch("galadril_vision.common.config.VisionConfig.from_yaml")
def test_vision_config_resource_parsing(mock_from_yaml: MagicMock) -> None:
    """Validates that yaml files parse into cached configuration properties cleanly."""
    mock_cfg = MagicMock()
    mock_pipeline_cfg = MagicMock()
    mock_cfg.to_pipeline_config.return_value = mock_pipeline_cfg
    mock_from_yaml.return_value = mock_cfg

    resource = VisionConfigResource(pipeline_path="test_bootstrap.yaml")

    assert resource.vision_config is mock_cfg
    mock_from_yaml.assert_called_once_with("test_bootstrap.yaml")

    # Ensure cached_property avoids duplicate parsing hits.
    assert resource.vision_config is mock_cfg
    mock_from_yaml.assert_called_once()

    assert resource.pipeline_config is mock_pipeline_cfg
    mock_cfg.to_pipeline_config.assert_called_once()
