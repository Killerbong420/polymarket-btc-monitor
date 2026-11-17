from forecast_guard import validate_official_target


def test_target_audit_does_not_mislabel_candle_closes_as_benchmark_ticks():
    result = validate_official_target(63701.65, {
        "opening_average": 63691.54,
        "opening_average_samples": 60,
        "window_start": 1000,
        "points": [(t, 63691.54) for t in range(940, 1000)],
    })
    assert "candle closes" in result["basis"]
    assert "benchmark ticks" not in result["basis"]
    assert result["official_target_authoritative"] is True
    assert result["official_target_usd"] == 63701.65
    assert result["reconstructed_opening_usd"] == 63691.54
    assert result["classification"] == "MATERIAL_DIVERGENCE"
    assert result["opening_sample_audit"]["observed_seconds"] == 60
