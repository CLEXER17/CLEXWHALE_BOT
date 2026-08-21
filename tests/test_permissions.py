"""Roles, capabilities and the handler guard.

Spec §36: "Admin permissions", "Co-admin permissions", "Public/private mode",
"Unauthorized users". Spec §29 fixes the matrix; spec §28 forbids a co-admin
from touching the admin list at all; spec §32 requires the Telegram *user id* —
never the username — to be the permanent identity.
"""

from __future__ import annotations

import pytest

from app.bot.middleware import permissions
from app.services.admin_service import (
    CO_ADMIN_CAPABILITIES,
    MAIN_ONLY_CAPABILITIES,
    PUBLIC_CAPABILITIES,
    ROLE_CO,
    ROLE_MAIN,
    ROLE_USER,
    AdminError,
    AdminService,
    Capability,
)
from tests.conftest import (
    CO_ADMIN_ID,
    MAIN_ADMIN_ID,
    STRANGER_ID,
    FakeContext,
    FakeUpdate,
)


async def promote(admins: AdminService, telegram_id: int = CO_ADMIN_ID) -> None:
    await admins.add_co_admin(MAIN_ADMIN_ID, telegram_id, username="helper")


# ── the capability matrix (§29) ─────────────────────────────────

async def test_main_admin_holds_every_capability(container):
    for capability in Capability:
        assert container.admins.can(MAIN_ADMIN_ID, capability) is True


async def test_co_admin_holds_the_shared_capabilities(container):
    await promote(container.admins)
    for capability in CO_ADMIN_CAPABILITIES:
        assert container.admins.can(CO_ADMIN_ID, capability) is True


async def test_co_admin_cannot_manage_admins_or_read_the_audit_log(container):
    """§28: adding or removing an admin is Main-Admin-only, full stop."""
    await promote(container.admins)
    for capability in MAIN_ONLY_CAPABILITIES:
        assert container.admins.can(CO_ADMIN_ID, capability) is False
    assert container.admins.can(CO_ADMIN_ID, Capability.MANAGE_ADMINS) is False


def test_the_two_capability_sets_do_not_overlap():
    """A structural guarantee: no future edit can leak MANAGE_ADMINS sideways."""
    assert CO_ADMIN_CAPABILITIES & MAIN_ONLY_CAPABILITIES == frozenset()
    assert Capability.MANAGE_ADMINS not in CO_ADMIN_CAPABILITIES


def test_public_capabilities_are_read_only():
    assert PUBLIC_CAPABILITIES == frozenset(
        {Capability.VIEW_PUBLIC, Capability.VIEW_WHALES}
    )
    for capability in PUBLIC_CAPABILITIES:
        assert capability not in MAIN_ONLY_CAPABILITIES


async def test_stranger_has_nothing_in_private_mode(container):
    for capability in Capability:
        assert container.admins.can(STRANGER_ID, capability, public_mode=False) is False


async def test_stranger_gets_read_access_in_public_mode(container):
    admins = container.admins
    assert admins.can(STRANGER_ID, Capability.VIEW_WHALES, public_mode=True) is True
    assert admins.can(STRANGER_ID, Capability.VIEW_PUBLIC, public_mode=True) is True
    # Public mode opens the *view*, never the controls.
    assert admins.can(STRANGER_ID, Capability.CONTROL_MONITORING, public_mode=True) is False
    assert admins.can(STRANGER_ID, Capability.CHANGE_THRESHOLD, public_mode=True) is False
    assert admins.can(STRANGER_ID, Capability.MANAGE_ADMINS, public_mode=True) is False


async def test_anonymous_caller_is_a_plain_user(container):
    """No id, no privileges. (Userless updates are dropped by the guard anyway.)"""
    admins = container.admins
    assert admins.role_of(None) == ROLE_USER
    assert admins.can(None, Capability.VIEW_WHALES, public_mode=False) is False
    assert admins.can(None, Capability.CHANGE_SETTINGS, public_mode=True) is False
    assert admins.can(None, Capability.MANAGE_ADMINS, public_mode=True) is False


# ── refusal messages ───────────────────────────────────────────

async def test_private_mode_refusal_is_the_specified_text(container):
    """§26: the exact wording the spec requires."""
    with pytest.raises(AdminError) as excinfo:
        container.admins.require(STRANGER_ID, Capability.VIEW_WHALES, public_mode=False)
    assert str(excinfo.value) == (
        "🔒 This bot is currently private.\n"
        "Whale monitoring is available only to authorized administrators."
    )


async def test_co_admin_refusal_explains_the_admin_rule(container):
    await promote(container.admins)
    with pytest.raises(AdminError) as excinfo:
        container.admins.require(CO_ADMIN_ID, Capability.MANAGE_ADMINS)
    message = str(excinfo.value)
    assert "Only the Main Admin can manage administrators" in message
    assert "Co-Admins cannot add or remove other admins" in message


async def test_require_returns_the_actor_when_authorized(container):
    actor = container.admins.require(MAIN_ADMIN_ID, Capability.MANAGE_ADMINS)
    assert actor.telegram_id == MAIN_ADMIN_ID
    assert actor.role == ROLE_MAIN
    assert actor.is_main_admin is True
    assert actor.role_label == "Main Admin"


async def test_co_admin_actor_labels_itself(container):
    await promote(container.admins)
    actor = container.admins.require(CO_ADMIN_ID, Capability.CHANGE_COINS)
    assert actor.role == ROLE_CO
    assert actor.is_admin is True
    assert actor.is_main_admin is False
    assert actor.role_label == "Co-Admin"


# ── admin management ───────────────────────────────────────────

async def test_main_admin_can_add_a_co_admin(container):
    actor = await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")
    assert actor.role == ROLE_CO
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO
    assert container.admins.co_admin_count == 1


async def test_co_admin_count_is_unlimited(container):
    """§28: "unlimited co-admins" — nothing in the service caps the number."""
    for offset in range(12):
        await container.admins.add_co_admin(MAIN_ADMIN_ID, 800_000_000 + offset)
    assert container.admins.co_admin_count == 12


async def test_a_co_admin_cannot_add_another_co_admin(container):
    await promote(container.admins)
    with pytest.raises(AdminError):
        await container.admins.add_co_admin(CO_ADMIN_ID, STRANGER_ID)
    assert container.admins.role_of(STRANGER_ID) == ROLE_USER


async def test_a_stranger_cannot_add_a_co_admin(container):
    with pytest.raises(AdminError):
        await container.admins.add_co_admin(STRANGER_ID, STRANGER_ID)
    assert container.admins.co_admin_count == 0


async def test_adding_the_main_admin_as_co_admin_is_refused(container):
    with pytest.raises(AdminError) as excinfo:
        await container.admins.add_co_admin(MAIN_ADMIN_ID, MAIN_ADMIN_ID)
    assert "already the Main Admin" in str(excinfo.value)


async def test_adding_an_existing_co_admin_is_refused(container):
    await promote(container.admins)
    with pytest.raises(AdminError) as excinfo:
        await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    assert "already a Co-Admin" in str(excinfo.value)


async def test_an_invalid_target_id_is_refused(container):
    for bad in (0, -1):
        with pytest.raises(AdminError) as excinfo:
            await container.admins.add_co_admin(MAIN_ADMIN_ID, bad)
        assert "Invalid Telegram user ID" in str(excinfo.value)


async def test_main_admin_can_remove_a_co_admin(container):
    await promote(container.admins)
    await container.admins.remove_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_USER
    assert container.admins.co_admin_count == 0


async def test_the_main_admin_cannot_be_removed_by_anyone(container):
    """§28/§29: not by a co-admin, and not by the main admin either."""
    await promote(container.admins)
    with pytest.raises(AdminError) as excinfo:
        await container.admins.remove_co_admin(MAIN_ADMIN_ID, MAIN_ADMIN_ID)
    assert "Main Admin cannot be removed" in str(excinfo.value)
    assert "MAIN_ADMIN_ID" in str(excinfo.value)

    with pytest.raises(AdminError):
        await container.admins.remove_co_admin(CO_ADMIN_ID, MAIN_ADMIN_ID)
    assert container.admins.role_of(MAIN_ADMIN_ID) == ROLE_MAIN


async def test_a_co_admin_cannot_remove_another_co_admin(container):
    await promote(container.admins, CO_ADMIN_ID)
    await promote(container.admins, 800_000_042)
    with pytest.raises(AdminError):
        await container.admins.remove_co_admin(CO_ADMIN_ID, 800_000_042)
    assert container.admins.role_of(800_000_042) == ROLE_CO


async def test_a_co_admin_cannot_remove_themselves(container):
    """§28: self-removal is an admin-list change, so it needs MANAGE_ADMINS."""
    await promote(container.admins)
    with pytest.raises(AdminError):
        await container.admins.remove_co_admin(CO_ADMIN_ID, CO_ADMIN_ID)
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO


async def test_removing_a_non_admin_is_refused(container):
    with pytest.raises(AdminError) as excinfo:
        await container.admins.remove_co_admin(MAIN_ADMIN_ID, STRANGER_ID)
    assert "not a Co-Admin" in str(excinfo.value)


# ── identity is the numeric id (§32) ───────────────────────────

async def test_role_survives_a_username_change(container):
    """The username is decoration; the id is the identity."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="old_handle")
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO
    actor = container.admins.actor(CO_ADMIN_ID, "brand_new_handle")
    assert actor.role == ROLE_CO
    assert actor.username == "brand_new_handle"


async def test_a_username_is_never_an_authorization_key(container):
    """Nothing in the service accepts a string handle as a caller identity."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")
    listed = await container.admins.list_admins(MAIN_ADMIN_ID)
    entry = next(row for row in listed if row["telegram_id"] == CO_ADMIN_ID)
    assert isinstance(entry["telegram_id"], int)
    assert entry["username"] == "helper"
    # A different id with the same handle gets nothing.
    assert container.admins.role_of(999_999_999) == ROLE_USER


async def test_roles_reload_from_the_database(container, database, env):
    """§48: a redeploy must not silently drop the co-admin list."""
    await promote(container.admins)
    fresh = AdminService(database, env)
    await fresh.load()
    assert fresh.role_of(CO_ADMIN_ID) == ROLE_CO
    assert fresh.role_of(MAIN_ADMIN_ID) == ROLE_MAIN


async def test_the_env_main_admin_wins_over_the_stored_role(container, database, env):
    """MAIN_ADMIN_ID is the deployment's root of trust, not a database row."""
    await promote(container.admins, 900_000_001)
    from tests.conftest import make_settings

    moved = make_settings(main_admin_id=900_000_001)
    fresh = AdminService(database, moved)
    await fresh.load()
    assert fresh.role_of(900_000_001) == ROLE_MAIN
    # The previous owner is demoted, not deleted, so the audit trail survives —
    # but they lose every main-admin-only capability immediately.
    assert fresh.role_of(MAIN_ADMIN_ID) == ROLE_CO
    assert fresh.can(MAIN_ADMIN_ID, Capability.MANAGE_ADMINS) is False


async def test_admin_ids_always_include_the_main_admin(container):
    assert container.admins.admin_ids == [MAIN_ADMIN_ID]
    await promote(container.admins)
    assert container.admins.admin_ids == sorted([MAIN_ADMIN_ID, CO_ADMIN_ID])


async def test_list_admins_requires_view_admins(container):
    with pytest.raises(AdminError):
        await container.admins.list_admins(STRANGER_ID)


async def test_list_admins_puts_the_main_admin_first(container):
    await promote(container.admins)
    rows = await container.admins.list_admins(MAIN_ADMIN_ID)
    assert rows[0]["telegram_id"] == MAIN_ADMIN_ID
    assert rows[0]["role"] == ROLE_MAIN


# ── the @requires guard ────────────────────────────────────────

@permissions.requires(Capability.CHANGE_THRESHOLD)
async def guarded(update, context, actor):
    context.chat_data["ran_as"] = actor.role
    return actor


async def test_guard_runs_the_handler_for_an_authorized_caller(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m")
    actor = await guarded(update, ctx)
    assert actor is not None
    assert ctx.chat_data["ran_as"] == ROLE_MAIN


async def test_guard_refuses_a_stranger_and_never_runs_the_handler(container, ctx):
    update = FakeUpdate(STRANGER_ID, text="/setthreshold 5m")
    assert await guarded(update, ctx) is None
    assert "ran_as" not in ctx.chat_data
    assert "currently private" in update.last


async def test_guard_refuses_a_co_admin_for_a_main_only_action(container, ctx):
    await promote(container.admins)

    @permissions.requires(Capability.MANAGE_ADMINS)
    async def admin_only(update, context, actor):
        context.chat_data["reached"] = True

    update = FakeUpdate(CO_ADMIN_ID, text="/addadmin 123")
    await admin_only(update, ctx)
    assert "reached" not in ctx.chat_data
    assert "Only the Main Admin" in update.last


async def test_guard_ignores_an_update_with_no_user(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID)
    update.effective_user = None
    assert await guarded(update, ctx) is None
    assert "ran_as" not in ctx.chat_data


async def test_guard_throttles_a_flood(container, ctx):
    """A script hammering the bot is told to slow down, not silently dropped."""
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m")
    for _ in range(11):
        await guarded(update, ctx)
    assert permissions.throttle(MAIN_ADMIN_ID) is False
    assert any("Too many" in text or "slow" in text.lower() for text in update.sent)


async def test_throttling_is_per_user(container, ctx):
    await promote(container.admins)
    for _ in range(11):
        await guarded(FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m"), ctx)
    assert permissions.throttle(MAIN_ADMIN_ID) is False
    assert permissions.throttle(CO_ADMIN_ID) is True


async def test_guard_registers_the_caller_for_alert_delivery(container, ctx):
    from app.database.repository import UserRepository

    await guarded(FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m", chat_id=4242), ctx)
    async with container.db.session() as session:
        row = await UserRepository.get(session, MAIN_ADMIN_ID)
    assert row is not None
    assert row.chat_id == 4242


async def test_registration_is_cached_so_it_does_not_write_on_every_command(container, ctx):
    calls: list[int] = []
    original = container.admins.register_user

    async def counting(**kwargs):
        calls.append(kwargs["telegram_id"])
        await original(**kwargs)

    container.admins.register_user = counting  # type: ignore[method-assign]
    for _ in range(5):
        await guarded(FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m"), ctx)
    assert calls == [MAIN_ADMIN_ID]


async def test_get_container_fails_loudly_when_unwired(container):
    empty = FakeContext(container)
    empty.bot_data.clear()
    with pytest.raises(RuntimeError, match="AppContainer"):
        permissions.get_container(empty)


# ── reply plumbing ─────────────────────────────────────────────

async def test_respond_sends_a_message_for_a_command(container):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/status")
    await permissions.respond(update, "<b>hello</b>")
    assert update.effective_chat.last == "<b>hello</b>"


async def test_respond_edits_in_place_for_a_callback(container):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="panel:open")
    await permissions.respond(update, "panel body")
    assert update.callback_query.edits == ["panel body"]
    assert update.effective_chat.sent == []


async def test_respond_can_be_forced_to_send_instead_of_edit(container):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="panel:open")
    await permissions.respond(update, "new message", edit=False)
    assert update.callback_query.edits == []
    assert update.effective_chat.last == "new message"


async def test_a_toast_is_answered_without_html_tags(container):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="mon:on")
    await permissions.respond(update, "body", toast="<b>Monitoring started</b>")
    assert update.callback_query.answers[0]["text"] == "Monitoring started"
    assert update.callback_query.answers[0]["show_alert"] is False


async def test_an_alert_is_shown_as_a_popup(container):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="mon:on")
    await permissions.respond(update, "body", alert="Careful")
    assert update.callback_query.answers[0]["show_alert"] is True


async def test_refusal_of_a_callback_is_a_popup_not_a_chat_message(container):
    update = FakeUpdate(STRANGER_ID, callback_data="adm:remove_id:1")
    await permissions.refuse(update, "🚫 <b>No</b>")
    assert update.callback_query.alerts == ["🚫 No"]
    assert update.effective_chat.sent == []


def test_plain_text_strips_markup_for_callback_alerts():
    assert permissions.plain_text("<b>bold</b> and <i>italic</i>") == "bold and italic"
    assert permissions.plain_text("no markup") == "no markup"


async def test_actor_of_does_not_enforce_a_capability(container):
    update = FakeUpdate(STRANGER_ID, text="/start")
    actor = permissions.actor_of(update, container)
    assert actor.role == ROLE_USER
    assert actor.telegram_id == STRANGER_ID
