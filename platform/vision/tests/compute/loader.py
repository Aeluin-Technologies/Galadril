"""Unit tests verifying dynamic module imports and model construction factories."""

from unittest.mock import MagicMock, patch

from galadril_vision.compute.loader import build_model, import_string


class TestLoaderModule:
    """Validates structural reflection components across local runtime spaces."""

    def test_import_string_success(self) -> None:
        """Verifies modules resolve correctly from absolute dot-separated paths."""
        with patch(
            "galadril_vision.compute.loader.importlib.import_module"
        ) as mock_import:
            mock_mod = MagicMock()
            mock_mod.TargetClass = "resolved_class_attribute"
            mock_import.return_value = mock_mod

            res = import_string("package.module.TargetClass")
            assert res == "resolved_class_attribute"
            mock_import.assert_called_once_with("package.module")

    def test_build_model_factory(self) -> None:
        """Validates parameter propagation during model factory setup tasks."""
        mock_class = MagicMock()
        mock_class.return_value = "constructed_instance"

        with patch(
            "galadril_vision.compute.loader.import_string",
            return_value=mock_class,
        ) as mock_imp:
            instance = build_model(
                "path.to.Class", artifact_path="/tmp/model", param="value"
            )
            assert instance == "constructed_instance"
            mock_imp.assert_called_once_with("path.to.Class")
            mock_class.assert_called_once_with(
                artifact_path="/tmp/model", param="value"
            )
