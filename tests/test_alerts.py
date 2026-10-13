import pytest
from pathlib import Path
from src.ops.alerts import AlertManager
from src.db.contracts import FakeSender


@pytest.mark.asyncio
async def test_alerts_delivery_and_deduplication(tmp_path: Path):
    sender = FakeSender()
    owner_ids = [111, 222]
    alert_mgr = AlertManager(sender, owner_ids, cooldown_s=3600, data_dir=str(tmp_path))

    # Send first alert
    await alert_mgr.send_alert("KEY_AUTH_ERROR", "Key 401 error")
    assert len(sender.sent_messages) == 2
    assert sender.sent_messages[0]["chat_id"] == 111
    assert sender.sent_messages[1]["chat_id"] == 222

    # Second alert of the same type within cooldown -> deduplicated, no new messages
    await alert_mgr.send_alert("KEY_AUTH_ERROR", "Key 401 error again")
    assert len(sender.sent_messages) == 2

    # Different event type -> sent
    await alert_mgr.send_alert("LOW_DISK", "Disk low")
    assert len(sender.sent_messages) == 4


@pytest.mark.asyncio
async def test_alerts_graceful_on_failure(tmp_path: Path):
    sender = FakeSender()
    sender.fail_send = True  # Simulates 403 or network failure
    alert_mgr = AlertManager(sender, [111], cooldown_s=3600, data_dir=str(tmp_path))

    # Should not raise exception
    await alert_mgr.send_alert("TEST_ERR", "Test message")


def test_clean_shutdown_marker(tmp_path: Path):
    alert_mgr = AlertManager(FakeSender(), [111], data_dir=str(tmp_path))

    # Initially missing -> unclean
    assert not alert_mgr.check_clean_shutdown_marker()

    # Write marker
    alert_mgr.write_clean_shutdown_marker()

    # Check -> clean, and removes marker
    assert alert_mgr.check_clean_shutdown_marker()
    assert not alert_mgr.check_clean_shutdown_marker()
