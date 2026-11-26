"""Offline tests for the new Milo systems. Run:  DISCORD_TOKEN=x python -m pytest tests -q
(No Discord or Upstash connection is needed: Discord objects are mocks and Upstash is an in-memory fake.)"""
import asyncio, json, os, random, sys, time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DISCORD_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord
import pytest
import bot

run = asyncio.run


# ------------------------------------------------------------------ fakes
class FakeStore(bot.UpstashStore):
    """In-memory stand-in for Upstash (strings, hashes, sets)."""
    def __init__(self):
        self.kv, self.h, self.sets, self.fail = {}, {}, {}, False

    async def _cmd(self, *a):
        if self.fail:
            raise bot.StoreError("down")
        op, args = a[0].upper(), a[1:]
        if op == "SET": self.kv[args[0]] = args[1]; return "OK"
        if op == "GET": return self.kv.get(args[0])
        if op == "DEL":
            for k in args: self.kv.pop(k, None); self.h.pop(k, None); self.sets.pop(k, None)
            return 1
        if op == "HSET": self.h.setdefault(args[0], {})[args[1]] = args[2]; return 1
        if op == "HSETNX":
            d = self.h.setdefault(args[0], {})
            if args[1] in d: return 0
            d[args[1]] = args[2]; return 1
        if op == "HDEL": return 1 if self.h.get(args[0], {}).pop(args[1], None) is not None else 0
        if op == "HGETALL": return [x for k, v in self.h.get(args[0], {}).items() for x in (k, v)]
        if op == "SADD":
            st = self.sets.setdefault(args[0], set())
            if args[1] in st: return 0
            st.add(args[1]); return 1
        if op == "SMEMBERS": return sorted(self.sets.get(args[0], set()))
        if op in ("HINCRBY", "ZADD", "ZREM", "ZINCRBY", "HMSET", "EXPIRE", "INCR", "DECR", "ZREMRANGEBYRANK"):
            return 1                                    # points-saving commands: accepted, not modelled
        raise AssertionError(f"fake store: unsupported {op}")

    async def pipeline(self, cmds):
        out = []
        for c in cmds:
            try: out.append({"result": await self._cmd(*c)})
            except bot.StoreError as e: out.append({"error": str(e)})
        return out


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Clean slate for every test."""
    store = FakeStore()
    monkeypatch.setattr(bot, "LIMIT_STORE", store)
    for name in ("balance", "earned", "daily", "shop", "stats", "pity", "jackpot", "last_spin"):
        d = getattr(bot.POINTS, name, None)
        if isinstance(d, dict): d.clear()
    bot.POINTS.frozen.clear() if hasattr(bot.POINTS, "frozen") else None
    bot.POINTS.ready = True
    bot.EXTRAS = bot.ExtraData()
    bot.EXTRAS.ready = True
    monkeypatch.setattr(bot, "GAMBLIN", None)
    monkeypatch.setattr(bot, "log_event", AsyncMock())
    monkeypatch.setattr(bot, "MANAGER_ROLE_ID", 111)
    monkeypatch.setattr(bot, "MILO_ROLE_ID", 222)
    yield store


def member(uid=1, roles=(), name="user"):
    m = MagicMock(spec=discord.Member)
    m.id, m.name, m.display_name, m.bot, m.mention = uid, name, name.title(), False, f"<@{uid}>"
    m.roles = [MagicMock(id=r) for r in roles]
    m.display_avatar.url = "https://cdn.example/a.png"
    m.joined_at = datetime(2024, 1, 1)
    return m


def interaction(user):
    i = MagicMock()
    i.user, i.guild = user, None
    i.response.send_message, i.response.edit_message = AsyncMock(), AsyncMock()
    i.followup.send, i.original_response = AsyncMock(), AsyncMock(return_value=MagicMock())
    i.edit_original_response = AsyncMock()
    return i


def message(content, author):
    m = MagicMock()
    m.content, m.author = content, author
    m.guild = MagicMock()
    m.delete, m.channel.send, m.attachments = AsyncMock(), AsyncMock(), []
    return m


def today():
    return bot._day_info()[0]


def sent_text(call):
    a, k = call
    return " ".join([str(x) for x in a] + [str(v) for v in k.values()])


# ------------------------------------------------------------------ catalog / prices / discount
def test_catalog_integrity_and_no_halloween():
    assert all(v["cat"] in bot.SHOP_CATEGORIES for v in bot.CATALOG.values())
    assert all(v["price"] for v in bot.CATALOG.values() if v["listed"])
    for i in ("daily_sunrise", "slots_cosmic", "win_legend", "daily_christmas", "daily_newyear", "slots_christmas", "slots_newyear", "tag_zealous"):
        assert i in bot.CATALOG
    assert not any("hallo" in i or "hallween" in i for i in bot.CATALOG)
    assert bot.CATALOG["slots_neon"]["price"] == 10_000 and bot.CATALOG["slots_royal"]["price"] == 20_000 and bot.CATALOG["slots_cosmic"]["price"] == 35_000
    assert bot.resolve_item_id("TITLE_ZEALOUS") == "tag_zealous"


def test_discount_math_and_targeting(monkeypatch):
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_PCT", 5); monkeypatch.setattr(bot, "SHOP_DISCOUNT_REASON", "Happy New Year")
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_ITEMS", None)
    assert bot.shop_price_info("daily_sunrise") == (10_000, 9_500, "5% Happy New Year")
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_ITEMS", {"slots_cosmic"})
    assert bot.shop_price_info("daily_sunrise") == (10_000, 10_000, None)
    assert bot.shop_price_info("slots_cosmic")[1] == 33_250
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_PCT", 95)
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_ITEMS", None)
    assert all(bot.shop_price(i) >= 1 for i, v in bot.CATALOG.items() if v["listed"])
    monkeypatch.setattr(bot, "SHOP_PRICE_PERCENT", 50); monkeypatch.setattr(bot, "SHOP_DISCOUNT_PCT", 0)
    assert bot.shop_price("daily_sunrise") == 5_000                    # existing env var still works
    with pytest.raises(KeyError):
        bot.shop_price("slots_christmas")                              # not for sale


def test_discount_env_parsing(monkeypatch):
    for raw, want in [("5, Happy New Year", (5, "Happy New Year")), ("10%,Sale", (10, "Sale")), ("5", (5, "Discount")),
                      ("", (0, "")), ("abc", (0, "")), ("0, x", (0, "")), ("-4, x", (0, "")), ("150, x", (0, ""))]:
        monkeypatch.setenv("SHOP_DISCOUNT", raw)
        assert bot._parse_shop_discount() == want, raw
    monkeypatch.setenv("SHOP_DISCOUNT_IDS", "0"); assert bot._parse_discount_ids() is None
    monkeypatch.setenv("SHOP_DISCOUNT_IDS", ""); assert bot._parse_discount_ids() is None
    monkeypatch.setenv("SHOP_DISCOUNT_IDS", "slots_cosmic, daily_sunrise,win_legend")
    assert bot._parse_discount_ids() == {"slots_cosmic", "daily_sunrise", "win_legend"}
    monkeypatch.setenv("SHOP_DISCOUNT_IDS", "nonsense"); assert bot._parse_discount_ids() == set()


def test_shop_display_shows_strikethrough_and_buy_uses_same_price(monkeypatch):
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_PCT", 5); monkeypatch.setattr(bot, "SHOP_DISCOUNT_REASON", "Happy New Year")
    monkeypatch.setattr(bot, "SHOP_DISCOUNT_ITEMS", {"daily_sunrise"})
    uid = 5; bot.POINTS.balance[uid] = 20_000
    text = "\n".join(f.value for f in bot._shop_cat_embed(uid, "daily", None).fields)
    assert "~~10,000" in text and "9,500" in text and "5% Happy New Year Discount" in text
    other = [f for f in bot._shop_cat_embed(uid, "daily", None).fields if "Treasure" in f.name][0].value
    assert "~~" not in other
    assert bot._shop_buy_error(uid, "daily_sunrise") is None
    i = interaction(member(uid)); v = bot.ShopConfirmView(uid, "daily", "daily_sunrise")
    run(v.confirm_btn.callback(i))
    assert bot.POINTS.points(uid) == 20_000 - 9_500 and bot.POINTS.owns(uid, "daily_sunrise")


def test_buy_equip_unowned_and_catalog_is_shared():
    uid = 7; bot.POINTS.balance[uid] = 10_000
    assert bot.POINTS.equip(uid, "slots", "slots_neon") is False          # not owned -> refused
    assert bot.style_of(uid, "slots") == "classic"
    bot.POINTS.buy_item(uid, "slots_neon", 10_000)
    assert bot.style_of(uid, "slots") == "slots_neon" and bot.POINTS.points(uid) == 0
    assert bot._shop_buy_error(uid, "slots_neon") == "You already own that one! Equip it from the shop."
    assert bot.POINTS.equip(uid, "slots", None) and bot.style_of(uid, "slots") == "classic"
    assert bot.POINTS.equip(uid, "slots", "slots_neon") and bot.style_of(uid, "slots") == "slots_neon"
    assert bot.POINTS.equip(uid, "daily", "slots_neon") is False          # wrong category
    assert bot.POINTS.shop[uid]["own"] == ["slots_neon"]                  # only the ID is stored, never a copy of the design
    bot.CATALOG["slots_neon"]["name"] = "Neon Arcade v2"                  # change the one shared definition...
    try:
        assert "Neon Arcade v2" in "\n".join(f.name for f in bot._shop_cat_embed(uid, "slots", None).fields)  # ...owners see it
    finally:
        bot.CATALOG["slots_neon"]["name"] = "Neon Arcade"
    assert bot._shop_buy_error(uid, "slots_christmas") == "That one isn't on sale right now."
    assert bot._shop_buy_error(8, "slots_cosmic").startswith("🛍️")        # can't afford


def test_titles_and_tags():
    uid = 9; bot.POINTS.balance[uid] = 5_000
    bot.POINTS.buy_item(uid, "title_lucky_paw", 2_500)
    assert bot._title_of(uid) == "Lucky Paw"
    assert bot.POINTS.equip(uid, "title", "title_high_roller") is False
    bot.POINTS.equip(uid, "title", None); assert bot._title_of(uid) is None
    bot.POINTS.grant_item(uid, "tag_zealous", equip=True)
    assert bot._tag_of(uid) == "❤️‍🔥 Zealous"


# ------------------------------------------------------------------ renderers
def test_every_design_renders_within_discord_limits():
    reels = ["7️⃣"] * 3
    for style in [i for i, v in bot.CATALOG.items() if v["cat"] == "slots"] + ["classic"]:
        for won in (True, False):
            e = bot._render_spin(style, reels, "headline", "spin", "footer", discord.Color.gold(), won)
            assert 0 < len(e) < 6000 and len(e.description) < 4096
    for style in [i for i, v in bot.CATALOG.items() if v["cat"] == "daily"] + ["classic"]:
        for streak in (1, 5, 7, 14):
            for lost, fee in ((False, 0), (True, 0), (False, 25)):
                e = bot._render_daily(style, "<@1>", bot._daily_reward(streak), streak, lost, 9, fee, "foot")
                assert len(e) < 6000
    for style in [i for i, v in bot.CATALOG.items() if v["cat"] == "jackpot"] + ["classic"]:
        text, e = bot._render_jackpot(style, 1, 100, "jackpot")
        assert (text is None) != (e is None)
    for i in bot.CATALOG:
        assert len(bot._shop_preview(1, i)) < 6000
    for cat, st in bot._CLASSIC_PREVIEW.items():
        assert len(bot._shop_preview(1, st)) < 6000


def test_daily_card_hierarchy_and_milestones():
    e = bot._render_daily("daily_gift", "<@1>", 35, 5, False, 0, 0, "f")
    assert [f.name for f in e.fields] == ["Streak", "Tomorrow", "Next milestone"]
    assert "+35" in e.description and "5" in e.fields[0].value and "bonus on day 7" in e.fields[2].value and "in 2 days" in e.fields[2].value
    assert "tomorrow" in bot._render_daily("daily_gift", "<@1>", 1, 6, False, 0, 0, "f").fields[2].value
    assert "claimed" in bot._render_daily("daily_gift", "<@1>", 1, 7, False, 0, 0, "f").fields[2].value
    assert bot._render_daily("daily_sunrise", "<@1>", 35, 3, False, 0, 0, "f").title == "🌅 Good morning!"   # reference design kept


def test_777_uses_equipped_style_and_default():
    bot.POINTS.grant_item(3, "jp777_royal", equip=True)
    posted = []
    ch = MagicMock(); ch.send = AsyncMock(side_effect=lambda **kw: posted.append(kw))
    old = bot.RESET_ANNOUNCE_CHANNEL_ID; bot.RESET_ANNOUNCE_CHANNEL_ID = 5
    bot.bot.get_channel = lambda _id: ch
    try:
        run(bot._post_jackpot_announcement(3, 700, ""))
        run(bot._post_jackpot_announcement(4, 700, ""))
    finally:
        bot.RESET_ANNOUNCE_CHANNEL_ID = old
    assert posted[0]["embed"] is not None and "ROYAL DECREE" in posted[0]["embed"].title
    assert posted[1]["embed"] is None and "JACKPOT" in posted[1]["content"]
    assert all(p["allowed_mentions"].everyone is False for p in posted)


# ------------------------------------------------------------------ daily + streak insurance
def set_streak(uid, days_ago, streak):
    d = (datetime.strptime(today(), "%Y-%m-%d") - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    bot.POINTS.daily[uid] = f"{d}:{streak}"


def test_normal_daily_claim_and_already_claimed():
    u = member(10); bot.POINTS.balance[10] = 100
    set_streak(10, 1, 4)
    i = interaction(u); run(bot.daily_slash.callback(i))
    assert bot.POINTS.daily_state(10) == (today(), 5) and bot.POINTS.points(10) == 100 + bot._daily_reward(5)
    assert i.response.send_message.call_args.kwargs["embed"].fields[0].value == "**5** days"
    i2 = interaction(u); run(bot.daily_slash.callback(i2))
    assert "already claimed" in sent_text(i2.response.send_message.call_args)
    assert bot.POINTS.points(10) == 100 + bot._daily_reward(5)


def test_insurance_fee_math(monkeypatch):
    set_streak(11, 3, 6)
    for bal, fee in ((1000, 50), (1001, 51), (1, 1), (19, 1), (21, 2)):
        bot.POINTS.balance[11] = bal; assert bot._insurance_offer(11, today())["fee"] == fee, bal
    bot.POINTS.balance[11] = 0; assert bot._insurance_offer(11, today()) is None
    monkeypatch.setattr(bot, "STREAK_INSURANCE_PERCENT", 12); bot.POINTS.balance[11] = 1000
    assert bot._insurance_offer(11, today())["fee"] == 120
    monkeypatch.setattr(bot, "STREAK_INSURANCE_PERCENT", 0); assert bot._insurance_offer(11, today()) is None
    monkeypatch.setattr(bot, "STREAK_INSURANCE_PERCENT", 5)
    set_streak(11, 30, 6); assert bot._insurance_offer(11, today()) is None          # too long ago
    set_streak(11, 3, 1); assert bot._insurance_offer(11, today()) is None           # nothing worth saving


def test_missed_daily_offers_insurance_and_charges_only_on_accept():
    u = member(12); bot.POINTS.balance[12] = 1000; set_streak(12, 3, 8)
    i = interaction(u); run(bot.daily_slash.callback(i))
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] and "missed" in kw["embed"].title.lower() and "50" in kw["embed"].fields[0].value
    assert bot.POINTS.points(12) == 1000 and bot.POINTS.daily_state(12)[0] != today()      # nothing happened yet
    view = kw["view"]
    i2 = interaction(u); run(view.restore_btn.callback(i2))
    assert bot.POINTS.points(12) == 1000 - 50 + bot._daily_reward(9)
    assert bot.POINTS.daily_state(12) == (today(), 9)
    card = i2.followup.send.call_args.kwargs["embed"]
    assert "restored" in card.description.lower() and "8" in card.description and card.fields[0].value == "**9** days"
    i3 = interaction(u); run(view.restore_btn.callback(i3))                                # double-click
    assert bot.POINTS.points(12) == 1000 - 50 + bot._daily_reward(9)
    i4 = interaction(u); run(bot.daily_slash.callback(i4))                                 # /daily again
    assert "already claimed" in sent_text(i4.response.send_message.call_args)
    assert bot.POINTS.points(12) == 1000 - 50 + bot._daily_reward(9)


def test_decline_insurance_resets_without_charge_and_cannot_restore_after():
    u = member(13); bot.POINTS.balance[13] = 1000; set_streak(13, 2, 8)
    i = interaction(u); run(bot.daily_slash.callback(i)); view = i.response.send_message.call_args.kwargs["view"]
    run(view.fresh_btn.callback(interaction(u)))
    assert bot.POINTS.daily_state(13) == (today(), 1) and bot.POINTS.points(13) == 1000 + bot._daily_reward(1)
    i2 = interaction(u); run(view.restore_btn.callback(i2))                                 # already used
    assert bot.POINTS.points(13) == 1000 + bot._daily_reward(1)
    assert bot._insurance_offer(13, today()) is None


def test_insurance_fee_change_requires_new_confirmation_and_other_user_blocked():
    u = member(14); bot.POINTS.balance[14] = 1000; set_streak(14, 2, 5)
    i = interaction(u); run(bot.daily_slash.callback(i)); view = i.response.send_message.call_args.kwargs["view"]
    bot.POINTS.balance[14] = 2000                                                           # balance moved -> fee would differ
    i2 = interaction(u); run(view.restore_btn.callback(i2))
    assert bot.POINTS.points(14) == 2000 and "fee changed" in sent_text(i2.response.edit_message.call_args)
    assert "100" in i2.response.edit_message.call_args.kwargs["embed"].fields[0].value
    intruder = interaction(member(99)); assert run(view.interaction_check(intruder)) is False


def test_timeout_does_not_charge_or_reset():
    u = member(15); bot.POINTS.balance[15] = 1000; set_streak(15, 2, 5)
    i = interaction(u); run(bot.daily_slash.callback(i)); view = i.response.send_message.call_args.kwargs["view"]
    view.message = MagicMock(); view.message.edit = AsyncMock()
    run(view.on_timeout())
    assert bot.POINTS.points(15) == 1000 and bot.POINTS.daily_state(15)[1] == 5
    assert "nothing was charged" in view.message.edit.call_args.kwargs["content"]


# ------------------------------------------------------------------ /pay
def test_pay_breakdown_is_exact():
    s, r = member(20, name="aapoke"), member(21, name="archo")
    bot.POINTS.balance[20] = 500; bot.POINTS.balance[21] = 0
    tx = bot.POINTS.record_pay(20, 21, 200, today(), bot.pay_fee(200))
    assert tx == {"sent": 200, "received": 200, "burned": 10, "deducted": 210}
    assert bot.POINTS.points(20) == 290 and bot.POINTS.points(21) == 200
    bot.POINTS.balance.update({30: 500, 31: 0})
    v = bot.PayConfirmView(member(30), member(31), 200); i = interaction(member(30)); run(v.confirm.callback(i))
    text = sent_text(i.followup.send.call_args)
    assert "Amount sent" in text and "200" in text and "Amount received" in text and "Amount burned" in text and "10" in text
    assert bot.POINTS.points(31) == 200 and bot.POINTS.points(30) == 290
    bot.PAY_FEE_PERCENT, old = 0, bot.PAY_FEE_PERCENT
    try:
        tx = bot.POINTS.record_pay(30, 31, 50, today(), bot.pay_fee(50))
        assert tx["burned"] == 0 and tx["received"] == 50 and tx["deducted"] == 50
    finally:
        bot.PAY_FEE_PERCENT = old


# ------------------------------------------------------------------ redeem
def test_default_codes():
    bot.POINTS.balance[40] = 10
    st, info = run(bot.EXTRAS.redeem(40, " milo_gifts "))
    assert st == "ok" and bot.POINTS.points(40) == 60
    assert run(bot.EXTRAS.redeem(40, "MILO_GIFTS"))[0] == "used" and bot.POINTS.points(40) == 60
    st, info = run(bot.EXTRAS.redeem(40, "MILO_TAG"))
    assert st == "ok" and bot.POINTS.owns(40, "tag_zealous") and "Zealous" in info["lines"][0]
    assert run(bot.EXTRAS.redeem(40, "MILO_TAG"))[0] == "used"
    assert run(bot.EXTRAS.redeem(41, "NOPE"))[0] == "invalid"
    assert run(bot.EXTRAS.redeem(41, "MILO_GIFTS"))[0] == "ok" and bot.POINTS.points(41) == 50     # other people can still use it


def test_redeem_race_only_one_succeeds(fresh):
    async def go():
        return await asyncio.gather(*[bot.EXTRAS.redeem(42, "MILO_GIFTS") for _ in range(8)])
    res = run(go())
    assert [r[0] for r in res].count("ok") == 1 and bot.POINTS.points(42) == 50


def test_redeem_survives_restart_and_database_decides(fresh):
    run(bot.EXTRAS.redeem(43, "MILO_GIFTS"))
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate())                                 # "restart"
    assert run(bot.EXTRAS.redeem(43, "MILO_GIFTS"))[0] == "used" and bot.POINTS.points(43) == 50
    bot.EXTRAS = bot.ExtraData(); bot.EXTRAS.ready = True                                   # cache knows nothing; the DB still says used
    assert run(bot.EXTRAS.redeem(43, "MILO_GIFTS"))[0] == "used" and bot.POINTS.points(43) == 50


def test_redeem_database_down_gives_nothing(fresh):
    fresh.fail = True
    st, _ = run(bot.EXTRAS.redeem(44, "MILO_GIFTS"))
    assert st == "error" and bot.POINTS.points(44) == 0
    fresh.fail = False
    assert run(bot.EXTRAS.redeem(44, "MILO_GIFTS"))[0] == "ok"


def mgr_msg(text, roles=(111,)):
    return message(text, member(1000, roles))


def replies(m):
    return " ".join(sent_text(c) for c in m.channel.send.call_args_list)


def test_addredeem_validates_everything_first(fresh):
    m = mgr_msg("?addredeem CHRISTMAS2026 +50,daily_christmas,slots_christmas")
    assert run(bot._handle_manager_command(m, "?addredeem")) is not False
    assert "created" in replies(m) and set(bot.EXTRAS.codes) == {"CHRISTMAS2026"}
    assert bot.EXTRAS.codes["CHRISTMAS2026"]["rewards"] == ["+50", "daily_christmas", "slots_christmas"]
    assert "CHRISTMAS2026" in fresh.h[bot.EXTRA_KEY_CODES]
    bad = mgr_msg("?addredeem BROKEN +50,daily_christmas,nope_id")
    run(bot._handle_manager_command(bad, "?addredeem"))
    assert "nope_id" in replies(bad) and "BROKEN" not in bot.EXTRAS.codes and "BROKEN" not in fresh.h[bot.EXTRA_KEY_CODES]
    dup = mgr_msg("?addredeem CHRISTMAS2026 +10"); run(bot._handle_manager_command(dup, "?addredeem"))
    assert "already exists" in replies(dup) and bot.EXTRAS.codes["CHRISTMAS2026"]["rewards"][0] == "+50"
    d2 = mgr_msg("?addredeem milo_gifts +9999"); run(bot._handle_manager_command(d2, "?addredeem")); assert "already exists" in replies(d2)
    for text in ("?addredeem X +50", "?addredeem GOOD_CODE +0", "?addredeem GOOD_CODE +5000000", "?addredeem GOOD_CODE daily_christmas,daily_christmas",
                 "?addredeem GOOD_CODE +50 expires:2020-01-01", "?addredeem GOOD_CODE"):
        mm = mgr_msg(text); run(bot._handle_manager_command(mm, "?addredeem")); assert "GOOD_CODE" not in bot.EXTRAS.codes, text
    m2 = mgr_msg("?addredeem NEWYEAR2027 +100,slots_newyear"); run(bot._handle_manager_command(m2, "?addredeem"))
    assert bot.EXTRAS.codes["NEWYEAR2027"]["expires"] == 0
    st, info = run(bot.EXTRAS.redeem(50, "newyear2027"))
    assert st == "ok" and bot.POINTS.points(50) == 100 and bot.POINTS.owns(50, "slots_newyear")
    assert run(bot.EXTRAS.redeem(50, "NEWYEAR2027"))[0] == "used"


def test_expiring_code_and_removal(fresh):
    fut = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    m = mgr_msg(f"?addredeem SOON +25 expires:{fut}"); run(bot._handle_manager_command(m, "?addredeem"))
    assert bot.EXTRAS.codes["SOON"]["expires"] > time.time()
    assert run(bot.EXTRAS.redeem(60, "SOON"))[0] == "ok"
    bot.EXTRAS.codes["SOON"]["expires"] = int(time.time()) - 5
    assert run(bot.EXTRAS.redeem(61, "SOON"))[0] == "expired"
    bot.EXTRAS.codes["SOON"]["expires"] = 0
    rm = mgr_msg("?removeredeem soon"); run(bot._handle_manager_command(rm, "?removeredeem"))
    assert "SOON" not in bot.EXTRAS.codes and "SOON" not in fresh.h[bot.EXTRA_KEY_CODES] and bot.EXTRA_KEY_USED + "SOON" not in fresh.sets
    assert run(bot.EXTRAS.redeem(62, "SOON"))[0] == "invalid"
    rd = mgr_msg("?removeredeem MILO_GIFTS"); run(bot._handle_manager_command(rd, "?removeredeem"))
    assert "permanent" in replies(rd) and run(bot.EXTRAS.redeem(63, "MILO_GIFTS"))[0] == "ok"


def test_cache_refreshes_from_database_after_changes(fresh):
    run(bot.EXTRAS.add_code("FROMDB", ["+5"], 0, 1))
    fresh.h[bot.EXTRA_KEY_CODES]["SNEAKY"] = json.dumps({"rewards": ["+7"], "expires": 0})    # changed elsewhere
    run(bot.EXTRAS.add_code("SECOND", ["+6"], 0, 1))
    assert {"FROMDB", "SECOND", "SNEAKY"} <= set(bot.EXTRAS.codes)                               # re-read, not just appended
    run(bot.EXTRAS.remove_code("FROMDB")); assert "FROMDB" not in bot.EXTRAS.codes


def test_retired_halloween_entries_are_dropped_on_load(fresh):
    fresh.h[bot.EXTRA_KEY_CODES] = {"SPOOKY": json.dumps({"rewards": ["+5", "halloween2026daily"], "expires": 0}),
                                    "FINE": json.dumps({"rewards": ["+5"], "expires": 0})}
    fresh.h[bot.EXTRA_KEY_LIMITED] = {"hallweenspinslot": json.dumps({"price": 9, "start": 1, "end": 2}),
                                      "slots_christmas": json.dumps({"price": 250, "start": 1, "end": 9999999999})}
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate())
    assert set(bot.EXTRAS.codes) == {"FINE"} and set(bot.EXTRAS.limited) == {"slots_christmas"}
    assert "SPOOKY" not in fresh.h[bot.EXTRA_KEY_CODES] and "hallweenspinslot" not in fresh.h[bot.EXTRA_KEY_LIMITED]
    bot.POINTS.shop[77] = bot._clean_shop_state({"own": ["halloween2026daily", "daily_gift"], "eq": {"daily": "halloween2026daily"}})
    assert bot.POINTS.shop[77] == {"own": ["daily_gift"], "eq": {}}


def test_redeem_slash(fresh):
    u = member(70); i = interaction(u)
    run(bot.redeem_slash.callback(i, "MILO_GIFTS")); assert bot.POINTS.points(70) == 50 and i.response.send_message.call_args.kwargs["ephemeral"]
    i = interaction(u); run(bot.redeem_slash.callback(i, "MILO_GIFTS")); assert "already redeemed" in sent_text(i.response.send_message.call_args)
    i = interaction(u); run(bot.redeem_slash.callback(i, "zzz")); assert "isn't valid" in sent_text(i.response.send_message.call_args)


# ------------------------------------------------------------------ limited shop
def ts(s, eod=False): return bot._parse_local_dt(s, end_of_day=eod)


def test_limited_shop_lifecycle(fresh):
    start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"); end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    m = mgr_msg(f"?limitedshop add slots_christmas 250 {start} {end}"); run(bot._handle_manager_command(m, "?limitedshop"))
    assert "added" in replies(m) and bot.shop_price("slots_christmas") == 250 and bot.shop_available("slots_christmas")
    assert "slots_christmas" in fresh.h[bot.EXTRA_KEY_LIMITED]
    dup = mgr_msg(f"?limitedshop add slots_christmas 300 {start} {end}"); run(bot._handle_manager_command(dup, "?limitedshop"))
    assert "already" in replies(dup) and bot.shop_price("slots_christmas") == 250
    p = mgr_msg("?limitedshop price slots_christmas 225"); run(bot._handle_manager_command(p, "?limitedshop")); assert bot.shop_price("slots_christmas") == 225
    t = mgr_msg(f"?limitedshop time slots_christmas {start} 2099-01-01 12:30"); run(bot._handle_manager_command(t, "?limitedshop"))
    assert bot.EXTRAS.limited["slots_christmas"]["end"] == ts("2099-01-01 12:30")
    uid = 80; bot.POINTS.balance[uid] = 1000
    assert "slots_christmas" in bot._cat_items(uid, "slots")
    assert bot._shop_buy_error(uid, "slots_christmas") is None
    bot.POINTS.buy_item(uid, "slots_christmas", bot.shop_price("slots_christmas"))
    # the selling period ends
    bot.EXTRAS.limited["slots_christmas"]["end"] = int(time.time()) - 1
    assert not bot.shop_available("slots_christmas") and "slots_christmas" in bot._cat_items(uid, "slots")      # owner still sees + uses it
    assert "slots_christmas" not in bot._cat_items(81, "slots") and bot._shop_buy_error(81, "slots_christmas") == "That one isn't on sale right now."
    assert bot.style_of(uid, "slots") == "slots_christmas" and "slots_christmas" in bot.CATALOG                 # ownership + design remain
    assert "slots_christmas" in "".join(e.fields[0].value for e in bot._customid_embeds()) or any("slots_christmas" in f.value for e in bot._customid_embeds() for f in e.fields)
    r = mgr_msg("?limitedshop remove slots_christmas"); run(bot._handle_manager_command(r, "?limitedshop"))
    assert "slots_christmas" not in bot.EXTRAS.limited and bot.POINTS.owns(uid, "slots_christmas") and "slots_christmas" in bot.CATALOG


def test_limited_shop_validation(fresh):
    cases = ["?limitedshop add slots_christmas 250 2026-12-31 2026-12-01", "?limitedshop add slots_christmas 0 2099-01-01 2099-02-01",
             "?limitedshop add slots_christmas abc 2099-01-01 2099-02-01", "?limitedshop add nope_id 5 2099-01-01 2099-02-01",
             "?limitedshop add slots_neon 5 2099-01-01 2099-02-01", "?limitedshop add slots_christmas 5 2099-13-01 2099-02-01",
             "?limitedshop add slots_christmas 5 2099-01-01", "?limitedshop add slots_christmas 5 2020-01-01 2020-02-01",
             "?limitedshop price slots_christmas 5", "?limitedshop remove slots_christmas", "?limitedshop time slots_christmas 2099-01-01 2099-02-01"]
    for c in cases:
        m = mgr_msg(c); run(bot._handle_manager_command(m, "?limitedshop"))
        assert "❌" in replies(m) or "Usage" in replies(m), c
    assert bot.EXTRAS.limited == {}


def test_limited_shop_survives_restart(fresh):
    run(bot.EXTRAS.limited_add("daily_newyear", 2027, int(time.time()) - 5, int(time.time()) + 9999, 1))
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate())
    assert bot.shop_price("daily_newyear") == 2027
    with pytest.raises(KeyError): bot.shop_price("daily_christmas")                    # only the selected New Year item is priced


def test_customid_lists_everything_even_unavailable():
    text = "\n".join(f.name + "\n" + f.value for e in bot._customid_embeds() for f in e.fields)
    for i in bot.CATALOG: assert f"`{i}`" in text, i
    assert all(len(e) < 6000 and all(len(f.value) <= 1024 for f in e.fields) for e in bot._customid_embeds())
    assert "MILO_GIFTS" in text and "+50" in text


# ------------------------------------------------------------------ seasons / leaderboard
def test_season_reset_keeps_everything_else(fresh):
    bot.POINTS.earned.update({1: 5000, 2: 3000, 3: 100}); bot.POINTS.balance.update({1: 777, 2: 888})
    assert bot.EXTRAS.season_label() == "SEASON 0" and [u for u, _ in bot.POINTS.qualified()] == [1, 2]
    ok, detail, old = run(bot.EXTRAS.reset_season("Christmas", "Season 01"))
    assert ok and old["label"] == "SEASON 0" and old["top"][0] == [1, 5000]
    assert bot.EXTRAS.season_label() == "Christmas · Season 01"
    assert bot.POINTS.earned == {1: 5000, 2: 3000, 3: 100} and bot.POINTS.balance[1] == 777 and bot.POINTS.balance[2] == 888      # untouched
    assert bot.POINTS.qualified() == [] and bot.POINTS.total_earned(1) == 5000
    bot.POINTS.earned[1] += 2500
    assert bot.POINTS.qualified() == [(1, 2500)] and bot.POINTS.rank_of(1) == 1
    run(bot.EXTRAS.reset_season("New Year", "Season 02"))
    assert [h["label"] for h in bot.EXTRAS.history] == ["SEASON 0", "Christmas · Season 01"]
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate())                                                              # restart
    assert bot.EXTRAS.season_label() == "New Year · Season 02" and len(bot.EXTRAS.history) == 2 and bot.EXTRAS.history[1]["top"] == [[1, 2500]]


def test_season_reset_failure_changes_nothing(fresh):
    bot.POINTS.earned[1] = 5000; fresh.fail = True
    ok, detail, _ = run(bot.EXTRAS.reset_season("X", "Y"))
    assert not ok and bot.EXTRAS.season_label() == "SEASON 0" and bot.EXTRAS.history == []


def test_resetleaderboard_command_and_leaderboard_embed(fresh):
    bot.POINTS.earned[1] = 5000
    m = mgr_msg("?resetleaderboard sname:Christmas snum:Season 01")
    run(bot._handle_manager_command(m, "?resetleaderboard"))
    view = m.channel.send.call_args.kwargs["view"]
    assert (view.name, view.num) == ("Christmas", "Season 01")
    i = interaction(member(1000, (111,))); run(view.confirm.callback(i))
    assert bot.EXTRAS.season_label() == "Christmas · Season 01"
    m2 = mgr_msg("?resetleaderboard snum:Season 02 sname:New Year"); run(bot._handle_manager_command(m2, "?resetleaderboard"))
    assert (m2.channel.send.call_args.kwargs["view"].name, m2.channel.send.call_args.kwargs["view"].num) == ("New Year", "Season 02")
    bad = mgr_msg("?resetleaderboard"); run(bot._handle_manager_command(bad, "?resetleaderboard")); assert "Usage" in replies(bad)
    bad2 = mgr_msg("?resetleaderboard sname:@everyone snum:1"); run(bot._handle_manager_command(bad2, "?resetleaderboard")); assert "❌" in replies(bad2)
    li = interaction(member(5)); run(bot.leaderboard_slash.callback(li, None))
    assert "Christmas · Season 01" in li.response.send_message.call_args.kwargs["embed"].title
    li = interaction(member(5)); run(bot.leaderboard_slash.callback(li, "SEASON 0"))
    assert "SEASON 0" in li.response.send_message.call_args.kwargs["embed"].title and "<@1>" in li.response.send_message.call_args.kwargs["embed"].description


# ------------------------------------------------------------------ permissions
FULL_ONLY = ["?give", "?set", "?undo", "?resetspins", "?mhelp", "?resetleaderboard", "?freeze", "?unfreeze", "?frozen", "?backup", "?restore",
             "?resetpreview", "?giveitem", "?takeitem", "?addredeem", "?removeredeem", "?redeems", "?customid", "?limitedshop"]
LIMITED_OK = ["?announce", "?snapshot", "?check", "?balances", "?mmhelp"]


def test_permission_matrix(fresh):
    assert set(bot._LIMITED_WORDS) == set(LIMITED_OK)
    assert set(FULL_ONLY) | set(LIMITED_OK) <= bot._MGR_WORDS | {"?mhelp"}
    for w in FULL_ONLY:
        for roles in ((222,), (), (333,)):
            m = mgr_msg(f"{w} x", roles)
            assert run(bot._handle_manager_command(m, w)) is False, (w, roles)
            m.delete.assert_not_called(); m.channel.send.assert_not_called()
    for w in LIMITED_OK:
        m = mgr_msg(f"{w} @x", (222,))
        assert run(bot._handle_manager_command(m, w)) is not False, w
        m.delete.assert_called()
        assert run(bot._handle_manager_command(mgr_msg(f"{w} @x", ()), w)) is False
    for w in FULL_ONLY + LIMITED_OK:                                     # a full manager can use everything
        m = mgr_msg(f"{w} x", (111,)); assert run(bot._handle_manager_command(m, w)) is not False, w


def test_limited_role_cannot_modify_even_with_real_arguments(fresh):
    bot.POINTS.balance[5] = 10
    for text, w in (("?give <@5> +500", "?give"), ("?addredeem EVIL +999", "?addredeem"), ("?limitedshop add slots_christmas 1 2099-01-01 2099-02-01", "?limitedshop"),
                    ("?limitedshop price slots_neon 1", "?limitedshop"), ("?resetleaderboard sname:a snum:b", "?resetleaderboard"), ("?set <@5> 99999", "?set")):
        run(bot._handle_manager_command(mgr_msg(text, (222,)), w))
    assert bot.POINTS.points(5) == 10 and "EVIL" not in bot.EXTRAS.codes and bot.EXTRAS.limited == {} and bot.EXTRAS.season_label() == "SEASON 0"


def test_send_and_gamblin_permissions(fresh, monkeypatch):
    m = mgr_msg("?send hello", (222,)); run(bot._handle_send_command(m)); m.channel.send.assert_called()      # Milo role keeps ?send
    m = mgr_msg("?send hello", (111,)); run(bot._handle_send_command(m)); m.channel.send.assert_called()
    m = mgr_msg("?send hello", (333,)); run(bot._handle_send_command(m)); m.delete.assert_not_called(); m.channel.send.assert_not_called()
    g = mgr_msg("?gamblin stop", (222,)); run(bot._handle_gamblin_command(g)); g.delete.assert_not_called(); g.channel.send.assert_not_called()
    g = mgr_msg("?gamblin duck", (222,)); run(bot._handle_gamblin_command(g)); g.delete.assert_not_called()
    g = mgr_msg("?gamblin schedule cancel", (222,)); run(bot._handle_gamblin_command(g)); g.delete.assert_not_called()
    g = mgr_msg("?gamblin schedule list", (222,)); run(bot._handle_gamblin_command(g)); g.channel.send.assert_called()
    g = mgr_msg("?gamblin schedule list", ()); run(bot._handle_gamblin_command(g)); g.channel.send.assert_not_called()


def test_help_menus_mention_everything(fresh):
    m = mgr_msg("?mhelp"); run(bot._handle_manager_command(m, "?mhelp"))
    emb = m.channel.send.call_args.kwargs["embed"]; txt = "\n".join(f.value for f in emb.fields)
    for needle in ("?addredeem", "?removeredeem", "?customid", "?limitedshop add", "?limitedshop price", "?limitedshop time", "?limitedshop remove",
                   "?gamblin schedule list", "?resetleaderboard sname:", "?give", "?check", "?freeze", "?announce"):
        assert needle in txt, needle
    assert len(emb) < 6000 and all(len(f.value) <= 1024 for f in emb.fields)
    m2 = mgr_msg("?mmhelp", (222,)); run(bot._handle_manager_command(m2, "?mmhelp"))
    t2 = "\n".join(f.value for f in m2.channel.send.call_args.kwargs["embed"].fields)
    assert all(x in t2 for x in LIMITED_OK[:4]) and "?gamblin schedule list" in t2 and "?send" in t2
    assert not any(x in t2 for x in ("?give", "?set", "?addredeem", "?limitedshop", "?resetleaderboard", "?freeze"))
    i = interaction(member(1)); run(bot.help_slash.callback(i)); h = "\n".join(f.value for f in i.response.send_message.call_args.kwargs["embed"].fields)
    assert "/redeem" in h and "/inspect" in h and "translate" not in h.lower()


def test_translate_is_gone():
    assert bot.bot.tree.get_command("translate") is None and not hasattr(bot, "translate_slash")
    assert bot.bot.tree.get_command("inspect") and bot.bot.tree.get_command("redeem")


# ------------------------------------------------------------------ daily Gamblin schedule
def day_ts(day, hh, mm): return int(datetime.strptime(f"{day} {hh}:{mm}", "%Y-%m-%d %H:%M").replace(tzinfo=bot._GAMES_TZ).timestamp())


def test_schedule_generation_many_seeds():
    day = "2030-06-15"; now = day_ts("2030-06-14", 12, 0)
    for seed in range(300):
        sch = bot._build_daily_schedule(day, now, random.Random(seed))
        g = sch["games"]; assert len(g) == 4
        fixed = [x for x in g if x["fixed"]]; rnd = [x for x in g if not x["fixed"]]
        assert len(fixed) == 1 and fixed[0]["ts"] == day_ts(day, 23, 0) and len(rnd) == 3
        for x in rnd: assert day_ts(day, 10, 50) <= x["ts"] <= day_ts(day, 23, 45)
        times = sorted(x["ts"] for x in g)
        assert all(b - a >= bot.DAILY_GAME_GAP_MIN * 60 for a, b in zip(times, times[1:])), seed        # no overlaps
        assert all(x["kind"] in bot.GAMBLIN_GAMES for x in g) and len({x["id"] for x in g}) == 4
        kinds = [x["kind"] for x in sorted(g, key=lambda x: x["ts"])]
        assert all(a != b for a, b in zip(kinds, kinds[1:]))


def test_schedule_env_changes(monkeypatch):
    day = "2030-06-15"; now = day_ts("2030-06-14", 12, 0)
    monkeypatch.setattr(bot, "DAILY_RANDOM_GAMES", 5)
    assert len(bot._build_daily_schedule(day, now, random.Random(1))["games"]) == 6
    monkeypatch.setattr(bot, "DAILY_RANDOM_GAMES", 0)
    g = bot._build_daily_schedule(day, now, random.Random(1))["games"]; assert len(g) == 1 and g[0]["fixed"]     # fixed game independent of the count
    monkeypatch.setattr(bot, "DAILY_RANDOM_GAMES", 3); monkeypatch.setattr(bot, "DAILY_START_MIN", 12 * 60); monkeypatch.setattr(bot, "DAILY_END_MIN", 13 * 60)
    for s in range(50):
        for x in bot._build_daily_schedule(day, now, random.Random(s))["games"]:
            assert x["fixed"] or day_ts(day, 12, 0) <= x["ts"] <= day_ts(day, 13, 0)
    monkeypatch.setattr(bot, "DAILY_FIXED_MIN", 21 * 60 + 30)
    assert [x for x in bot._build_daily_schedule(day, now, random.Random(2))["games"] if x["fixed"]][0]["ts"] == day_ts(day, 21, 30)
    monkeypatch.setattr(bot, "DAILY_FIXED_MIN", None)
    assert not any(x["fixed"] for x in bot._build_daily_schedule(day, now, random.Random(2))["games"])
    for raw, default, want in (("9:05", "10:50", 545), ("25:00", "10:50", 650), ("abc", "23:00", 1380), ("", "23:45", 1425)):
        monkeypatch.setenv("DAILY_GAMES_START_TIME", raw); assert bot._parse_hhmm("DAILY_GAMES_START_TIME", default) == want, raw


def test_schedule_generated_late_only_uses_future():
    day = "2030-06-15"; now = day_ts(day, 19, 0)
    g = bot._build_daily_schedule(day, now, random.Random(3))["games"]
    assert g and all(x["ts"] > now for x in g)
    late = bot._build_daily_schedule(day, day_ts(day, 23, 30), random.Random(3))["games"]
    assert not any(x["fixed"] for x in late)                                  # 11 PM already passed by more than the grace


def test_ensure_daily_is_once_per_day_and_survives_restart(fresh, monkeypatch):
    monkeypatch.setattr(bot, "_games_day", lambda now=None: "2030-06-15")
    first = run(bot.EXTRAS.ensure_daily()); snap = json.dumps(bot.EXTRAS.daily, sort_keys=True)
    assert first and json.loads(fresh.kv[bot.EXTRA_KEY_DAILYG])["day"] == "2030-06-15"
    async def many(): await asyncio.gather(*[bot.EXTRAS.ensure_daily() for _ in range(10)])
    run(many()); assert json.dumps(bot.EXTRAS.daily, sort_keys=True) == snap
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate()); run(bot.EXTRAS.ensure_daily())            # restart/redeploy
    assert json.dumps(bot.EXTRAS.daily, sort_keys=True) == snap
    monkeypatch.setattr(bot, "_games_day", lambda now=None: "2030-06-16")                              # next day -> new schedule
    run(bot.EXTRAS.ensure_daily()); assert bot.EXTRAS.daily["day"] == "2030-06-16"
    bot.EXTRAS = bot.ExtraData(); bot.EXTRAS.ready = False; assert run(bot.EXTRAS.ensure_daily()) is False   # never before the database is read


def test_schedule_save_retry_when_database_down(fresh, monkeypatch):
    monkeypatch.setattr(bot, "_games_day", lambda now=None: "2030-06-15"); fresh.fail = True
    run(bot.EXTRAS.ensure_daily()); assert bot.EXTRAS.daily["games"] and bot.EXTRAS._dirty
    fresh.fail = False; run(bot.EXTRAS.flush()); assert not bot.EXTRAS._dirty and bot.EXTRA_KEY_DAILYG in fresh.kv


def setup_games(monkeypatch, games):
    ch = MagicMock(); ch.send = AsyncMock()
    monkeypatch.setattr(bot, "_gamblin_channel", AsyncMock(return_value=ch))
    bot.EXTRAS.daily = {"day": bot._games_day(), "games": games}
    return ch


def G(i, delta, kind="duck", status="pending", fixed=False): return {"id": f"g{i}", "ts": int(time.time()) + delta, "kind": kind, "fixed": fixed, "status": status}


def test_due_game_starts_once_and_next_game_announced_without_everyone(fresh, monkeypatch):
    ch = setup_games(monkeypatch, [G(1, -5), G(2, 7200, "bj"), G(3, 20000, "roulette", fixed=True)])
    starts = []
    async def fake_start(kind, mid, daily_id=None): starts.append((kind, daily_id)); return ch, None
    monkeypatch.setattr(bot, "_start_gamblin", fake_start)
    run(bot._run_due_daily()); run(bot._run_due_daily())
    assert starts == [("duck", "g1")] and bot.EXTRAS.daily["games"][0]["status"] == "started"       # not twice
    assert json.loads(fresh.kv[bot.EXTRA_KEY_DAILYG])["games"][0]["status"] == "started"              # saved BEFORE it could be started again after a restart
    run(bot._after_daily_game("g1")); run(bot._after_daily_game("g1"))                              # finished (twice: still announced once)
    assert bot.EXTRAS.daily["games"][0]["status"] == "done" and ch.send.await_count == 1
    text = ch.send.call_args.args[0]; assert "next game" in text.lower() and "Blackjack" in text and ":R>" in text
    assert "@everyone" not in text and ch.send.call_args.kwargs["allowed_mentions"].everyone is False
    run(bot._after_daily_game("g2")); assert "Roulette" in ch.send.call_args.args[0] and ch.send.await_count == 2
    run(bot._after_daily_game("g3")); assert ch.send.await_count == 2                                 # nothing left to announce


def test_missed_games_are_skipped_not_started(fresh, monkeypatch):
    ch = setup_games(monkeypatch, [G(1, -3600), G(2, -1200, "bj"), G(3, 3000, "slots")])
    start = AsyncMock(return_value=(ch, None)); monkeypatch.setattr(bot, "_start_gamblin", start)
    run(bot._run_due_daily())
    assert [g["status"] for g in bot.EXTRAS.daily["games"]] == ["missed", "missed", "pending"] and start.await_count == 0
    assert ch.send.await_count == 1 and "Slots" in ch.send.call_args.args[0]                          # tells people what's next


def test_game_waits_while_another_is_running_then_is_skipped(fresh, monkeypatch):
    ch = setup_games(monkeypatch, [G(1, -30)])
    start = AsyncMock(return_value=(ch, None)); monkeypatch.setattr(bot, "_start_gamblin", start)
    monkeypatch.setattr(bot, "GAMBLIN", MagicMock())
    run(bot._run_due_daily()); assert start.await_count == 0 and bot.EXTRAS.daily["games"][0]["status"] == "pending"
    bot.EXTRAS.daily["games"][0]["ts"] = int(time.time()) - 700
    run(bot._run_due_daily()); assert bot.EXTRAS.daily["games"][0]["status"] == "missed"


def test_failed_start_goes_back_to_pending(fresh, monkeypatch):
    ch = setup_games(monkeypatch, [G(1, -5)])
    monkeypatch.setattr(bot, "_start_gamblin", AsyncMock(return_value=(None, "❌ no channel")))
    run(bot._run_due_daily()); assert bot.EXTRAS.daily["games"][0]["status"] == "pending"


def test_schedule_appears_in_midnight_and_lottery_embeds_and_list(fresh, monkeypatch):
    setup_games(monkeypatch, [G(1, 3600, "duck"), G(2, 9000, "bj", fixed=True)])
    e = bot._reset_announce_embed()
    f = [x for x in e.fields if "Gamblin games" in x.name][0]
    assert f.value.index("Duck Race") < f.value.index("Blackjack") and "<t:" in f.value and ":R>" in f.value
    r = {"day": "2030-06-15", "nums": [1, 2, 3], "tickets": 4, "w3": 0, "w2": 1, "w1": 2, "carry": 500, "jackpot": 0, "top": []}
    posted = []
    ch = MagicMock(); ch.send = AsyncMock(side_effect=lambda *a, **k: posted.append(k))
    monkeypatch.setattr(bot, "LOTTERY_CHANNEL_ID", 5); monkeypatch.setattr(bot.bot, "get_channel", lambda _id: ch)
    run(bot._post_lottery_result(r))
    assert posted and any("Gamblin games" in f.name for f in posted[0]["embed"].fields)
    g = mgr_msg("?gamblin schedule list", (222,)); run(bot._handle_gamblin_command(g))
    assert "Duck Race" in replies(g)


def test_scheduler_is_started_once_and_daily_end_callback(monkeypatch):
    import inspect
    src = inspect.getsource(bot.MiloClient.setup_hook)
    assert src.count("_daily_games_loop()") == 1 and src.count("EXTRAS.start()") == 1


# ------------------------------------------------------------------ profile
def test_inspect_profile():
    u = member(90, name="archo"); bot.POINTS.balance[90] = 1234; bot.POINTS.earned[90] = 5000
    bot.POINTS.record_session(90, "Duck Race", 300, 450, 100)
    bot.POINTS.grant_item(90, "title_high_roller", equip=True); bot.POINTS.grant_item(90, "tag_zealous", equip=True); bot.POINTS.grant_item(90, "slots_neon", equip=True)
    set_streak(90, 1, 6)
    i = interaction(u); run(bot.inspect_slash.callback(i, None))
    e = i.response.send_message.call_args.kwargs["embed"]
    assert e.thumbnail.url == "https://cdn.example/a.png" and e.title == "Archo"
    assert "@archo" in e.description and "Zealous" in e.description and "High Roller" in e.description
    f = {x.name: x.value for x in e.fields}
    assert "1.2K" in f["Balance"] or "1,234" in f["Balance"]
    assert "+450" in f["Biggest Gamblin win"] and "6" in f["Daily streak"] and "day 7" in f["Daily streak"] and "Neon Arcade" in f["Equipped"]
    other = member(91, name="other"); i = interaction(u); run(bot.inspect_slash.callback(i, other))
    assert i.response.send_message.call_args.kwargs["embed"].title == "Other"
    assert len(e) < 6000


# ------------------------------------------------------------------ integration-style checks
def test_real_start_path_and_done_callback_announce_next(fresh, monkeypatch):
    """_start_gamblin -> session task -> done-callback -> 'next game' announcement, using the real wiring."""
    ch = MagicMock(spec=discord.TextChannel); ch.send = AsyncMock(); ch.mention = "#gamblin"
    monkeypatch.setattr(bot, "_gamblin_channel", AsyncMock(return_value=ch))
    monkeypatch.setattr(bot, "_gamblin_preflight", AsyncMock(return_value=(ch, None)))

    class Stub:
        EMOJI, NAME, shutting_down = "🦆", "Stub", False
        def __init__(self, channel, started_by): self.task = None
        async def run(self): await asyncio.sleep(0.01)
    monkeypatch.setitem(bot.GAMBLIN_GAMES, "duck", ("🦆", "Duck Race", Stub))
    bot.EXTRAS.daily = {"day": bot._games_day(), "games": [G(1, -5, "duck"), G(2, 5000, "bj")]}

    async def go():
        await bot._run_due_daily()
        await asyncio.sleep(0.2)                       # let the stub finish + the callback task run
    run(go())
    assert bot.EXTRAS.daily["games"][0]["status"] == "done"
    assert ch.send.await_count == 1 and "Blackjack" in ch.send.call_args.args[0] and "@everyone" not in ch.send.call_args.args[0]


def test_shutdown_does_not_announce_next(fresh, monkeypatch):
    ch = MagicMock(); ch.send = AsyncMock(); monkeypatch.setattr(bot, "_gamblin_channel", AsyncMock(return_value=ch))
    bot.EXTRAS.daily = {"day": bot._games_day(), "games": [G(1, -5), G(2, 5000, "bj")]}
    sess = MagicMock(); sess.shutting_down = True
    async def go(): bot._daily_game_ended(sess, "g1"); await asyncio.sleep(0.05)
    run(go()); assert ch.send.await_count == 0 and bot.EXTRAS.daily["games"][0]["status"] == "pending"


def test_points_flush_also_saves_queued_extras(fresh, monkeypatch):
    fresh.fail = True; run(bot.EXTRAS.save_key("milo:test", "v")); assert bot.EXTRAS._dirty
    fresh.fail = False
    async def go(): return await bot.POINTS.flush("test")
    run(go()); assert fresh.kv.get("milo:test") == "v" and not bot.EXTRAS._dirty


def test_every_shop_screen_works_end_to_end(fresh):
    uid = 95; bot.POINTS.balance[uid] = 500_000; u = member(uid)
    i = interaction(u); run(bot.shop_slash.callback(i)); home = i.response.send_message.call_args.kwargs["view"]
    assert len(home.children) == len(bot.SHOP_CATEGORIES) and len(i.response.send_message.call_args.kwargs["embed"].fields) == len(bot.SHOP_CATEGORIES)
    for cat in bot.SHOP_CATEGORIES:
        ii = interaction(u); run(home._open(cat)(ii)); view = ii.response.edit_message.call_args.kwargs["view"]
        embed = ii.response.edit_message.call_args.kwargs["embed"]
        assert len(embed) < 6000 and len(embed.fields) <= 25 and len(view.select.options) <= 25
        for opt in view.select.options:
            view.select._values = [opt.value]; view.sel = opt.value
            run(view._picked(interaction(u)))
            run(view._preview(interaction(u)))                                   # preview every design, free
            assert bot.POINTS.points(uid) == 500_000
    assert bot.POINTS.shop.get(uid) is None
    # buy two items from different categories, equip/unequip through the buttons
    for cat, item in (("jackpot", "jp777_neon"), ("title", "title_lucky_paw")):
        v = bot.ShopCategoryView(uid, cat, item); ii = interaction(u); run(v._action(ii))
        c = ii.response.edit_message.call_args.kwargs["view"]; ii2 = interaction(u); run(c.confirm_btn.callback(ii2))
        assert bot.POINTS.owns(uid, item) and bot.style_of(uid, cat) == item
    v = bot.ShopCategoryView(uid, "title", "classic"); run(v._action(interaction(u))); assert bot._title_of(uid) is None
    v = bot.ShopCategoryView(uid, "title", "title_lucky_paw"); run(v._action(interaction(u))); assert bot._title_of(uid) == "Lucky Paw"


def test_double_purchase_charges_once(fresh):
    uid = 96; bot.POINTS.balance[uid] = 25_000; u = member(uid)
    a, b = bot.ShopConfirmView(uid, "slots", "slots_neon"), bot.ShopConfirmView(uid, "slots", "slots_neon")
    run(a.confirm_btn.callback(interaction(u))); run(b.confirm_btn.callback(interaction(u)))
    assert bot.POINTS.points(uid) == 15_000


def test_frozen_player_cannot_redeem_or_buy(fresh):
    bot.POINTS.frozen.add(97); bot.POINTS.balance[97] = 99_999
    i = interaction(member(97)); run(bot.redeem_slash.callback(i, "MILO_GIFTS"))
    assert bot.POINTS.points(97) == 99_999 and "frozen" in sent_text(i.response.send_message.call_args).lower()
    assert bot._shop_buy_error(97, "slots_neon")


def test_state_loaded_from_old_database_values_still_works(fresh):
    """Existing saved shop inventories (old ids) keep working under the new catalog."""
    old = {"own": ["daily_sunrise", "slots_neon", "win_champion"], "eq": {"daily": "daily_sunrise", "slots": "slots_neon"}}
    assert bot._clean_shop_state(old) == {"own": sorted(old["own"]), "eq": old["eq"]}


def all_text(e):
    return (e.title or "") + (e.description or "") + "".join(f.name + f.value for f in e.fields)


def test_no_literal_backslash_n_anywhere_in_player_text(fresh):
    u = member(120, name="archo"); bot.POINTS.balance[120] = 5; bot.POINTS.record_session(120, "Duck Race", 300, 450, 100)
    bot.POINTS.record_session(120, "Duck Race", 10, 10, 10); bot.POINTS.grant_item(120, "title_lucky_paw", equip=True)
    i = interaction(u); run(bot.inspect_slash.callback(i, None)); assert "\\n" not in all_text(i.response.send_message.call_args.kwargs["embed"])
    i = interaction(u); run(bot.redeem_slash.callback(i, "MILO_TAG")); assert "\\n" not in all_text(i.response.send_message.call_args.kwargs["embed"])
    i = interaction(u); run(bot.customize_slash.callback(i)); assert "\\n" not in sent_text(i.response.send_message.call_args)
    src = open(bot.__file__, encoding="utf-8").read()
    assert "\\\\n" not in src


def test_inspect_favourite_default_most_played_and_customize(fresh):
    u = member(121, name="archo"); fav = lambda: {f.name: f.value for f in bot._profile_embed(u).fields}.get("Favourite game")
    assert fav() is None                                                       # no games, nothing chosen
    bot.POINTS.record_session(121, "Duck Race", 1, 1, 1); bot.POINTS.record_session(121, "Duck Race", 1, 1, 1)
    bot.POINTS.record_session(121, "Roulette", 1, 1, 1)
    assert fav() == "🦆 Duck Race\n2 played"                                   # automatic = most played
    i = interaction(u); run(bot.customize_slash.callback(i)); view = i.response.send_message.call_args.kwargs["view"]
    assert [o.value for o in view.select.options] == ["auto", "slots", "bj", "duck", "roulette"] and view.select.options[0].default
    view.select._values = ["roulette"]; ii = interaction(u); run(view._picked(ii))
    assert fav() == "🎡 Roulette\n1 played" and bot.EXTRAS.fav[121] == "roulette"
    assert "Roulette" in ii.response.edit_message.call_args.kwargs["content"]
    view.select._values = ["slots"]; run(view._picked(interaction(u)))          # chosen game never played: still shown
    assert fav() == "🎰 Slots"
    assert run(view.interaction_check(interaction(member(999)))) is False        # other people can't use your menu
    bot.EXTRAS = bot.ExtraData(); run(bot.EXTRAS.hydrate()); assert bot.EXTRAS.fav[121] == "slots"        # restart: still saved
    view2 = bot.CustomizeView(121); view2.select._values = ["auto"]; run(view2._picked(interaction(u)))
    assert 121 not in bot.EXTRAS.fav and fav().startswith("🦆 Duck Race")        # back to automatic
    fresh.fail = True; view2.select._values = ["bj"]; ii = interaction(u); run(view2._picked(ii))
    assert 121 not in bot.EXTRAS.fav and "Nothing changed" in sent_text(ii.response.send_message.call_args)
    assert bot.bot.tree.get_command("customize")
