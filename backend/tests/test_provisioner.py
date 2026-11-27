"""Provisioner 测试（积分池版）。

mock 掉 IDC client 与 app.pool，验证：开通 5+1 步顺序与分步错误定位、
预付池无空余席位时成功但带 warning、停用 / 恢复 / 回收各自的副作用。
"""
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.provisioner as P  # noqa: E402


@pytest.fixture
def idc():
    client = MagicMock()
    client.create_user.return_value = {"UserId": "uid-1"}
    client.get_group_id.return_value = {"GroupId": "g-1"}
    session = MagicMock()
    session.client.return_value = client
    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.provisioner.get_frozen_credentials", return_value=MagicMock()), \
         patch("app.provisioner.kiro_api.send_password_email") as pwd:
        client.pwd = pwd
        yield client


def test_provision_happy_path_postpaid(idc):
    with patch("app.provisioner.pool.add_member", return_value=True) as add, \
         patch("app.provisioner.pool.assign_seat", return_value=(True, "AUTO")), \
         patch("app.provisioner.pool.set_limit") as setl:
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a")
    assert r.success is True and r.user_id == "uid-1" and r.warning == ""
    assert r.steps_succeeded == ["user_created", "group_added", "password_email_sent",
                                 "pool_joined", "seat_auto"]
    add.assert_called_once_with("uid-1")
    idc.pwd.assert_called_once()
    setl.assert_not_called()  # 没申请上限就不设


def test_provision_with_limit_sets_user_limit(idc):
    with patch("app.provisioner.pool.add_member", return_value=True), \
         patch("app.provisioner.pool.assign_seat", return_value=(True, "")), \
         patch("app.provisioner.pool.set_limit") as setl:
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a", usage_limit=800)
    assert r.success is True
    assert r.steps_succeeded[-2:] == ["seat_assigned", "limit_set"]
    setl.assert_called_once_with("USER", "uid-1", 800)


def test_provision_prepaid_no_seat_succeeds_with_warning(idc):
    with patch("app.provisioner.pool.add_member", return_value=True), \
         patch("app.provisioner.pool.assign_seat", return_value=(False, "CAPACITY_EXHAUSTED")):
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a")
    assert r.success is True
    assert "awaiting_seat" in r.steps_succeeded
    assert "没有空余席位" in r.warning


def test_provision_join_pool_failure_is_located(idc):
    from app.pool import PoolError
    with patch("app.provisioner.pool.add_member",
               side_effect=PoolError("预付积分池还没有购买席位")):
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a")
    assert r.success is False and r.error_step == "join_pool"
    assert r.user_id == "uid-1"  # 前几步已完成，重试时幂等
    assert "预付积分池还没有购买席位" in r.error


def test_provision_duplicate_username_blocks(idc):
    idc.create_user.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "exists"}}, "CreateUser")
    with patch("app.provisioner.pool.add_member") as add:
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a")
    assert r.success is False and r.error_step == "create_user"
    assert "已存在" in r.error
    add.assert_not_called()


def test_provision_group_already_member_is_idempotent(idc):
    idc.create_group_membership.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "dup"}}, "CreateGroupMembership")
    with patch("app.provisioner.pool.add_member", return_value=False), \
         patch("app.provisioner.pool.assign_seat", return_value=(True, "AUTO")):
        r = P.provision("zhangsan", "l@x.com", "De", "Li", "team-a")
    assert r.success is True
    assert "group_already_member" in r.steps_succeeded
    assert "pool_already_member" in r.steps_succeeded


def test_set_user_limit_wraps_errors():
    with patch("app.provisioner.pool.set_limit") as setl:
        assert P.set_user_limit("u1", 1000).success is True
    setl.assert_called_once_with("USER", "u1", 1000)
    with patch("app.provisioner.pool.set_limit", side_effect=RuntimeError("超过总量")):
        r = P.set_user_limit("u1", 99999)
    assert r.success is False and "超过总量" in r.error


def test_deactivate_removes_from_pool_and_marks_cancelled():
    mapping = MagicMock()
    with patch("app.mapping_store.MappingStore", return_value=mapping), \
         patch("app.provisioner.pool.remove_member") as rm:
        res = P.deactivate(["u1", "u2"])
    assert all(r["success"] for r in res)
    assert rm.call_args_list == [call("u1"), call("u2")]
    mapping.update_fields.assert_any_call("u1", status="cancelled")
    mapping.delete.assert_not_called()  # 停用保留映射


def test_deactivate_partial_failure_isolated():
    mapping = MagicMock()
    with patch("app.mapping_store.MappingStore", return_value=mapping), \
         patch("app.provisioner.pool.remove_member",
               side_effect=[None, RuntimeError("denied")]):
        res = P.deactivate(["u1", "u2"])
    assert res[0]["success"] is True
    assert res[1]["success"] is False and "denied" in res[1]["error"]


def test_reactivate_rejoins_and_reports_seat_warning():
    mapping = MagicMock()
    with patch("app.mapping_store.MappingStore", return_value=mapping), \
         patch("app.provisioner.pool.add_member") as add, \
         patch("app.provisioner.pool.assign_seat",
               side_effect=[(True, "AUTO"), (False, "CAPACITY_EXHAUSTED")]):
        res = P.reactivate(["u1", "u2"])
    assert add.call_count == 2
    assert res[0]["success"] and res[0]["warning"] == ""
    assert res[1]["success"] and "没有空余席位" in res[1]["warning"]
    mapping.update_fields.assert_any_call("u1", status="active")


def test_bulk_cancel_removes_pool_limit_user_and_mapping():
    """回收：移出池 + 清个人上限 + 删 IDC 用户 + 删映射，避免孤儿记录。"""
    client = MagicMock()
    session = MagicMock()
    session.client.return_value = client
    mapping = MagicMock()
    with patch("app.provisioner.get_session", return_value=session), \
         patch("app.provisioner.get_identity_store_id", return_value="d-1"), \
         patch("app.mapping_store.MappingStore", return_value=mapping), \
         patch("app.provisioner.pool.list_limits",
               return_value=[{"scope": "USER", "scope_id": "u1", "limit": 100.0}]), \
         patch("app.provisioner.pool.set_limit") as setl, \
         patch("app.provisioner.pool.remove_member") as rm:
        res = P.bulk_cancel(["u1", "u2"])
    assert all(r["success"] for r in res)
    assert rm.call_count == 2
    setl.assert_called_once_with("USER", "u1", None)  # 只清有个人上限的
    client.delete_user.assert_any_call(IdentityStoreId="d-1", UserId="u2")
    assert mapping.delete.call_count == 2


def test_retry_seats_reports_per_user():
    with patch("app.provisioner.pool.assign_seat",
               side_effect=[(True, ""), (False, "CAPACITY_EXHAUSTED")]):
        res = P.retry_seats(["u1", "u2"])
    assert res[0]["success"] is True
    assert res[1]["success"] is False and "没有空余席位" in res[1]["error"]
