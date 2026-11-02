import pytest

from forecaster.config import Config


def test_defaults_are_the_experiment_values():
    c = Config()
    assert c.season == 20
    assert c.clamp_rps == 280
    assert (c.lead_seconds, c.window_seconds) == (60, 60)


def test_from_env_parses_types():
    c = Config.from_env({"MODELS": "holtwinters, naive", "LEAD_SECONDS": "90",
                         "UPPER_QUANTILE": "0.95", "RPS_PER_POD": "42.5"})
    assert c.models == ("holtwinters", "naive")
    assert c.lead_seconds == 90
    assert c.upper_quantile == 0.95
    assert c.rps_per_pod == 42.5


@pytest.mark.parametrize("kw", [{"lead_seconds": 50}, {"cycle_seconds": 310},
                                {"min_history_cycles": 4}, {"upper_quantile": 0.4}])
def test_invalid(kw):
    with pytest.raises(ValueError):
        Config(**kw)
