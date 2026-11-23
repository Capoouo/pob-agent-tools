"""L0 deterministic assistant-reply scanner for PoE1 3.30 anti-hallucination hooks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .build_gate import URL_GATE_RULES
from .rules_legacy import LEGACY_PATCH_TRAPS_POE1
from .rules_poe2 import (
    POE2_ONLY_ASCENDANCIES_EN,
    POE2_ONLY_REVERSE_TRAPS_FOR_POB1,
    POE2_ONLY_TIMELESS_JEWELS_EN,
    POE2_ONLY_TIMELESS_JEWELS_ZH,
)
from .warnings import (
    EQUIPMENT_SLOT_ALIASES,
    _match_rule_fields,
    match_phrase_zh,
    match_word_en,
    normalize_equipment_slot,
    normalize_guardrail_text,
    normalize_zh_compact,
)


@dataclass(frozen=True)
class ReplyIssue:
    rule_id: str
    category: str
    trigger: str
    sentence: str
    reason: str
    remediation: str
    ground_terms: tuple[str, ...] = ()


_SENTENCE_SPLIT_RE = re.compile(
    r"[。！？；\n!?;]+|[,，]\s*(?=(?:所以|因此|于是|只能靠|请改用|改用|推荐连|建议连|可以连|直接连|"
    r"(?:so|therefore)\s+))",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+|pob2://[^\s<>\"']+", re.IGNORECASE)

_NEGATION_EXEMPT_RE = re.compile(
    r"(?:移除|删了|删除|绝版|没有|不存在|不可用|不是|并非|不能|切勿|别用|不要|无需|无效|"
    r"不吃|不受|不会|取代|替代|改为|仅限|已改为|改为固定|不生效|不算|不属于|无法降低|"
    r"是错的|不对|错了|这就错了|错误|误区|误解|辟谣|已?失效|无用|没用|没有用|会?(?:算错|出错)|凭空|误算|假阈值|"
    r"正确[（(]\s*33\s*ms|(?:若|如果|假如|要是)按[^\n。；;]{0,24}(?:则|会|导致|计算|算|换算)|"
    r"已经没了|没了|早已移除|已删除|已删|"
    r"不可得|已绝版|无法(?:再)?获取|打不到|掉不了|买不到|刷不到|(?:属于|已是)遗产|不再掉落|不再存在|"
    r"别挂|别连|别带|别堆|别在|不要挂|不要连|不要带|不要堆|"
    r"(?:是|属于|仅在|只在)[^\n。；;]{0,12}\bpoe2\b|\bpoe2\s*(?:专属|独有|特有|机制|资源)|"
    r"=\s*0\s*(?:收益|效果|作用|提升)|0\s*(?:收益|效果|作用|提升)|零收益|零效果|毫无(?:收益|效果|作用)|"
    r"天生上限\s*3|自然上限\s*3|上限为?\s*3\s*级?|永远到不了|到不了\s*20|"
    r"\blegacy_removed_328\b|\bdrop[- ]disabled\b|\bunobtainable\b|\bnot\s+obtainable\b|"
    r"\bis\s+wrong\b|\bwrong\s+in\s+poe1\b|"
    r"\bremoved\b|\bno\s+longer\b|\bnot\s+in\s+poe1\b|\bzero\s+effect\b|"
    r"\bdoes\s+not\b|\bdoesn't\b|\bdon't\b|\bdo\s+not\b|\bnever\b|\binstead\s+of\b|"
    r"\bnot\s+a\s+curse\b|\bisn't\b|\bis\s+not\b)",
    re.IGNORECASE,
)
_AWAKE_3_RE = re.compile(
    r"(?:\b(?:empower|enhance|enlighten)\b|赋予|賦予|赋能|賦能|增幅|增强|增強|启蒙|啟蒙)",
    re.IGNORECASE,
)
_EXC_OVER_LV5_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:lv\.?|level)\s*(?:[6-9]|[12]\d)\b|(?:[6-9]|[12]\d)\s*(?:级|級)|\b2[01]/2[03]\b)",
    re.IGNORECASE,
)
_INTERROGATIVE_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s+.*(?:吗|么|呢|对不对|行不行|能不能|可不可|是否|[?？])\s*$"
)
_ALT_QUALITY_REPLY_RE = re.compile(
    r"(?:\b(?:anomalous|divergent|phantasmal|alternate\s+quality|alt[- ]quality|regrading\s+lens)\b|"
    r"(?:异常|分歧|魅影|替代)品质|异化宝石|(?:异常|分歧|魅影)的[\u4e00-\u9fff]|"
    r"(?:^|[\s「『(（])(?:异常|分歧|魅影)\s*[:：]\s*[\u4e00-\u9fff])",
    re.IGNORECASE,
)
_SINGLE_SUPPORT_DEBUNK_RE = re.compile(
    r"(?:没有.{0,8}限制|允许重复|可以重复|可重复|不像\s*poe2|"
    r"\bunlike\s+poe2\b|\bno\s+.*limit\b|\bnot\s+.*limit\b|\bare\s+legal\b|\bis\s+legal\b|\ballowed\b)",
    re.IGNORECASE,
)

_EXCLUDED_POE2_IDS = frozenset({"POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"})
_AMBIGUOUS_ASC_EN = frozenset({"titan", "oracle", "shaman", "amazon", "lich"})
_FREE_TEXT_ASC_ZH = (
    "风暴编织者", "时空术士", "古灵使徒", "宝石军团", "血法师", "鲜血法师",
    "狱火师", "奇塔弗铁匠", "奇塔弗的铁匠", "夏乌拉侍僧", "夏乌拉的侍僧", "猎巫人", "祈求者",
)
_HARD_REMOVED_LEGACY_IDS = frozenset({
    "PATCH_328_AWAKENED_REMOVED",
    "PATCH_325_DIVINE_BLESSING_REMOVED",
    "PATCH_325_ANCESTRAL_TOTEMS_REMOVED",
    "PATCH_323_LAB_ENCHANTS_REMOVED",
    "PATCH_323_ALT_QUALITY_REMOVED",
    "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS",
    "PATCH_324_SEXTANTS_REMOVED",
})

_SPIRIT_EN_CTX_RE = re.compile(
    r"(?:\b(?:stack|reserve|reserving|spend|gain|more|extra|total|unreserved)\s+spirit\b|"
    r"\bspirit\s+(?:pool|cost|gem|gems|reservation|reserve|requirement)\b)",
    re.IGNORECASE,
)
_SPIRIT_EN_EXCL_RE = re.compile(
    r"spirit\s+shield|spirit\s+offering|raging\s+spirit|spectral\s+spirit|spirit\s+burst|spirit\s+charge",
    re.IGNORECASE,
)
_DISTILLED_EN_CTX_RE = re.compile(r"\bdistilled\s+(?:emotion|emotions|anoint|oil|oils)\b", re.IGNORECASE)
_DISTILLED_EN_EXCL_RE = re.compile(r"distilled\s+perfection", re.IGNORECASE)
_CHARM_CTX_RE = re.compile(r"(?:\bcharm\s+(?:slot|slots|belt)\b|护符槽|咒符槽|腰带护符|腰带咒符)", re.IGNORECASE)

_POE2_EB_TERM_RE = re.compile(r"(?:\beldritch\s+battery\b|\beb\b|异能魔力)", re.IGNORECASE)
_POE2_EB_FALLACY_RE = re.compile(
    r"(?:(?:把|将)?(?:能量)?护盾转(?:化|换)?为(?:最大)?(?:魔力|蓝量)|"
    r"(?:把|将)?(?:最大)?(?:魔力|蓝量)转(?:化|换)?为(?:最大)?(?:能量)?(?:护盾|es)|"
    r"转(?:化|换)?为(?:最大)?(?:魔力|护盾|es)|"
    r"\bconverts?\s+(?:all\s+)?(?:energy\s+shield|es)\s+(?:in)?to\s+(?:maximum\s+)?mana\b|"
    r"\bconverts?\s+(?:all\s+)?(?:maximum\s+)?mana\s+(?:in)?to\s+(?:maximum\s+)?(?:energy\s+shield|es)\b|"
    r"\bmana[- ]to[- ]es\b|\bes[- ]to[- ]mana\b)",
    re.IGNORECASE,
)
_POE2_SINGLE_SUPPORT_RE = re.compile(
    r"(?:(?:不能|不可|无法|禁止)重复(?:使用|带|插|镶嵌|连)?(?:同一个|相同)?辅助(?:宝石|石)?|"
    r"(?:辅助(?:宝石|石)?|同一(?:颗|个)辅助).{0,12}(?:全(?:角色|身)|每个角色)?只能(?:带|插|用|装备)(?:一颗|一个|1颗|1个)|"
    r"单辅助限制|"
    r"\bonly\s+(?:use|socket|equip|have)\s+(?:one|1)\s+(?:copy\s+of\s+(?:each|a)\s+)?support\s+gem\b|"
    r"\b(?:cannot|can't)\s+(?:use|socket|equip|have)\s+duplicate\s+support\s+gems?\b|"
    r"\bone[- ]per[- ]character\s+support\s+limit\b|\bsingle[- ]support\s+limit\b)",
    re.IGNORECASE,
)

# (rule_id, category, term_re_or_None, trigger_re, reason, remediation, ground_terms)
_FALLACY_SPECS: tuple[tuple[str, str, re.Pattern[str] | None, re.Pattern[str], str, str, tuple[str, ...]], ...] = (
    (
        "EXCEPTIONAL_SUPPORT_LEVEL_CAP",
        "exceptional_level",
        re.compile(
            r"(?:\bexceptional\s*(?:supports?|gems?|辅助|宝石|石)|\bgreater\s+(?:spell\s+echo|multistrike|spell\s+cascade|"
            r"multiple\s+projectiles)\b|卓越辅助|卓越宝石|卓越石|高阶施法回响|高阶多重打击|高阶法术凝聚)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:(?<![A-Za-z0-9])(?:lv\.?|level)\s*(?:[5-9]|[12]\d)\b|(?:[5-9]|[12]\d)\s*(?:级|級)|"
            r"(?:升到|升至|拉到)\s*(?:[5-9]|[12]\d)(?:\s*(?:级|級))?|\b2[01]/2[03]\b)",
            re.IGNORECASE,
        ),
        "3.28+ Exceptional Supports cap at natural Lv3 (corrupted Lv4), never Lv5+ or Lv20.",
        "Correct Exceptional Support level to Lv1-3 (corrupted Lv4).",
        ("Greater Spell Echo",),
    ),
    (
        "FALLACY_SERVER_TICK_33_33MS",
        "mechanic_fallacy",
        None,
        re.compile(r"(?:33\.33\s*ms|0\.0333+\s*s|1\s*/\s*30\s*(?:s|秒|帧))", re.IGNORECASE),
        "PoE1 server tick is 0.033s (33ms), NOT 33.33ms (1/30s).",
        "Recalculate trigger breakpoints with 0.033s tick (`pob data tick`).",
        ("server tick",),
    ),
    (
        "FALLACY_CWC_SCALES_WITH_CDR",
        "mechanic_fallacy",
        re.compile(r"(?:\bcast\s+while\s+channelling\b|\bcwc\b|吟唱时施放|引导时施放)", re.IGNORECASE),
        re.compile(r"(?:\bcdr\b|\bcooldown\s+recovery\b|冷却回复|冷却缩减|冷却速度|帧档位|冷却断点)", re.IGNORECASE),
        "Cast while Channelling (CwC) uses a fixed channelling interval and does NOT scale with CDR.",
        "Remove CDR advice for CwC; only gem level changes its trigger interval.",
        ("Cast while Channelling",),
    ),
    (
        "FALLACY_RAMAKO_RES_LOWERING",
        "mechanic_fallacy",
        re.compile(r"(?:\bramako\b|雷默科|烈阳之光|烈陽之光|太阳之光|太陽之光)", re.IGNORECASE),
        re.compile(
            r"(?:\bflammability\b|\belemental\s+weakness\b|\bexposure\b|\bscorch\b|"
            r"易燃|元素要害|曝露|曝光|焦灼|减火抗|降低火(?:焰)?抗(?:性)?)",
            re.IGNORECASE,
        ),
        "Ramako sets nearby enemies' Fire Res vs DoT to fixed -20% while stationary; Flammability/Ele Weakness/Exposure have zero effect.",
        "Use Punishment or defensive curses instead for stationary Ramako Fire DoT.",
        ("Ramako",),
    ),
    (
        "FALLACY_FROST_BOMB_AS_CURSE",
        "mechanic_fallacy",
        re.compile(r"(?:\bfrost\s+bomb\b|寒霜爆)", re.IGNORECASE),
        re.compile(
            r"(?:(?:作为|当成|当作|算作|属于)诅咒|占(?:用)?(?:一个|1个)?诅咒(?:上限|位)?|诅咒上限|"
            r"\bis\s+a\s+curse\b|\bas\s+a\s+curse\b|\bcounts?\s+as\s+a\s+curse\b|"
            r"\bcurse\s+(?:limit|slot)\b|\buses?\s+a\s+curse\b)",
            re.IGNORECASE,
        ),
        "Frost Bomb is an Exposure/debuff spell, NOT a Curse, and does not use curse limit.",
        "Correct Frost Bomb classification: it is not a Curse.",
        ("Frost Bomb",),
    ),
)

_EXTRA_SLOT_ALIASES: dict[str, str] = {
    "1号戒指": "Ring 1", "1 号戒指": "Ring 1", "一号戒指": "Ring 1", "戒指一": "Ring 1",
    "2号戒指": "Ring 2", "2 号戒指": "Ring 2", "二号戒指": "Ring 2", "戒指二": "Ring 2",
    "3号戒指": "Ring 3", "3 号戒指": "Ring 3", "三号戒指": "Ring 3",
}
_OPEN_AFFIX_CLAIM_RE = re.compile(
    r"(?:空前缀|空后缀|空词缀|还有空位|有空余词缀|有空词缀|有空位|可(?:以)?直接工艺|工艺台补|工艺补|"
    r"\bopen\s+(?:prefix|suffix|affix)\b|\bfree\s+(?:prefix|suffix|affix)\b|\bcraft(?:able)?\s+(?:prefix|suffix|affix)\b)",
    re.IGNORECASE,
)
_OPEN_AFFIX_EXEMPT_RE = re.compile(
    r"(?:先检查|先看|不确定|确认是否有|先确认|需要确认|看(?:一下|看)?有没有空|"
    r"如果有空|若有空|若满词缀|如果还有空|前提是有空|没有空(?:前缀|后缀|词缀)|已满词缀|词缀已满|"
    r"\bcheck\s+if\b|\bif\s+[^\n。；;]{0,20}\bopen\b|\bassuming\s+[^\n。；;]{0,20}\bopen\b|"
    r"\bwhether\s+[^\n。；;]{0,20}\bopen\b|\bno\s+open\s+(?:prefix|suffix|affix)\b|\bfull\s+affixes\b)",
    re.IGNORECASE,
)

_PANEL_PATTERNS = (
    re.compile(
        r"\d+(?:\.\d+)?\s*(?:M|K|万|亿)\s*(?:DPS|EHP|秒伤|有效生命|总伤|击中伤害|持续伤害|点燃\s*DPS|中毒\s*DPS)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:DPS|EHP|秒伤|总秒伤|有效生命|满血有效生命)\s*(?:[:：=≈]|约为?|达到?|高达)\s*\d+(?:\.\d+)?\s*(?:M|K|万|亿)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:DPS|EHP|总伤|秒伤|满血有效生命|有效生命)[^\n。；;，,]{0,16}?"
        r"(?:[+-]\d+(?:\.\d+)?%|(?:提升|提高|增加|增长|降低|减少|下降|少了|多了)\s*(?:约\s*)?\d+(?:\.\d+)?%)|"
        r"[+-]\d+(?:\.\d+)?%\s*(?:more\s+)?(?:DPS|EHP|总伤|秒伤|有效生命))",
        re.IGNORECASE,
    ),
)

_TRIGGER_CTX_RE = re.compile(
    r"(?:\bcoc\b|暴击时施放|\bcospri\b|冰剑|\bmjolner\b|\bmjölner\b|雷锤|\bpoet\b|诗人|"
    r"\bcwdt\b|受伤时施放|\bsword\s+of\s+ages\b|万古之剑|万古剑|触发冷却|触发器|触发上限|触发频率)",
    re.IGNORECASE,
)
_TRIGGER_NUM_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:APS|次/秒|次每秒|/s)|"
    r"(?:攻速上限|触发上限)[^\n。；;]{0,12}\d+(?:\.\d+)?|"
    r"\d{1,3}\s*%[^\n。；;]{0,16}(?:CDR|冷却回复|冷却缩减|断点|档位|帧)|"
    r"(?:CDR|冷却回复|断点|档位)[^\n。；;]{0,16}\d{1,3}\s*%)",
    re.IGNORECASE,
)

_BUILD_SOURCE_PATTERNS = (
    re.compile(r"@([^\s\"'<>]+?\.(?:xml|code))(?=\s|$|[,，。；;)\]}])", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?pobb\.in/[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?poe\.ninja/(?:poe1|builds|pob)/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"https?://(?:api\.)?pob\.codes/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"(?:[A-Za-z]:[/\\]|(?:\.\.?[/\\]))[^\s\"'<>]+?\.(?:xml|code)(?=\s|$|[,，。；;)\]}])", re.IGNORECASE),
)
_BUILD_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])e[NJ][A-Za-z0-9+/_=-]{78,}(?![A-Za-z0-9+/_=-])")


def _strip_blockquotes(text: str) -> str:
    return "\n".join(
        ln
        for ln in (text or "").splitlines()
        if not ln.lstrip().startswith(">") and not _INTERROGATIVE_HEADING_RE.match(ln)
    )


def _split_sentences(text: str) -> list[str]:
    return [seg.strip() for seg in _SENTENCE_SPLIT_RE.split(_strip_blockquotes(text)) if seg.strip()]


def _scan_sentence_poe2(
    sentence: str, norm: str, zh_compact: str, seen: set[str], issues: list[ReplyIssue]
) -> None:
    def _add(rule_id: str, trigger: str, reason: str, remediation: str, terms: tuple[str, ...] = ()) -> None:
        if rule_id not in seen:
            seen.add(rule_id)
            issues.append(ReplyIssue(rule_id, "poe2", trigger, sentence, reason, remediation, terms or (trigger,)))

    if not _NEGATION_EXEMPT_RE.search(sentence):
        for url_m in _URL_RE.finditer(sentence):
            cand = url_m.group(0).rstrip(".,);]")
            for ur in URL_GATE_RULES:
                if ur.belongs_to == "poe2" and ur.pattern.search(cand):
                    _add(ur.rule_id, cand, f"URL matches {ur.label} (PoE2), not PoE1.", ur.counterpart_hint, (cand,))

        for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
            if rule.rule_id in _EXCLUDED_POE2_IDS or rule.rule_id in seen:
                continue
            hit = _match_rule_fields(
                sentence, norm, norm, zh_compact,
                en_exact=rule.en_exact, zh_exact=rule.zh_exact, en_words=rule.en_words,
                zh_phrases=rule.zh_phrases, regex=rule.regex,
                exclude_en_re=rule.exclude_en_re, exclude_zh_compounds=rule.exclude_zh_compounds,
            )
            if hit is None and rule.rule_id == "POE2_SPIRIT_RESERVATION":
                hit = match_phrase_zh(sentence, "精魂", ("精魂之盾",))
                if hit is None and _SPIRIT_EN_CTX_RE.search(norm) and not _SPIRIT_EN_EXCL_RE.search(norm):
                    hit = "spirit"
            elif hit is None and rule.rule_id == "POE2_WAYSTONE":
                hit = match_word_en(norm, "waystone", rule.exclude_en_re) or match_word_en(
                    norm, "waystones", rule.exclude_en_re
                )
            elif hit is None and rule.rule_id == "POE2_DISTILLED_EMOTIONS":
                if _DISTILLED_EN_CTX_RE.search(norm) and not _DISTILLED_EN_EXCL_RE.search(norm):
                    hit = "distilled"
            elif hit is None and rule.rule_id == "POE2_CHARM_BELT_SLOTS":
                cm = _CHARM_CTX_RE.search(sentence)
                hit = cm.group(0) if cm else None
            if hit is not None:
                _add(rule.rule_id, hit, rule.error, rule.remediation, (hit,))

        if "POE2_TIMELESS_JEWEL_IN_POB1" not in seen:
            tj = next((p for p in POE2_ONLY_TIMELESS_JEWELS_EN if match_word_en(norm, p) is not None), None)
            if tj is None:
                tj = next((z for z in POE2_ONLY_TIMELESS_JEWELS_ZH if match_phrase_zh(sentence, z) is not None), None)
            if tj is not None:
                _add(
                    "POE2_TIMELESS_JEWEL_IN_POB1", tj,
                    f"`{tj}` is a PoE2 timeless/radius jewel and does not exist in PoE1.",
                    "Use `pob timeless list` for valid PoE1 Historic jewels.", (tj,),
                )

        if "POE2_CLASS_IN_POB1" not in seen:
            asc = next(
                (a for a in sorted(POE2_ONLY_ASCENDANCIES_EN, key=len, reverse=True)
                 if a not in _AMBIGUOUS_ASC_EN and match_word_en(norm, a) is not None),
                None,
            ) or next((z for z in _FREE_TEXT_ASC_ZH if match_phrase_zh(sentence, z) is not None), None)
            if asc is not None:
                _add(
                    "POE2_CLASS_IN_POB1", asc,
                    f"`{asc}` is a PoE2-only ascendancy and does not exist in PoE1.",
                    "PoE1 classes: Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion.", (asc,),
                )

        if "POE2_EB_MANA_CONVERSION" not in seen and _POE2_EB_TERM_RE.search(sentence):
            eb_m = _POE2_EB_FALLACY_RE.search(sentence)
            if eb_m is not None:
                _add(
                    "POE2_EB_MANA_CONVERSION", eb_m.group(0),
                    "PoE1 Eldritch Battery protects Mana with ES; it does NOT convert ES<->Mana (PoE2).",
                    "State PoE1 EB: ES protects Mana and pays skill Mana costs (50% less ES Recharge Rate).",
                    ("Eldritch Battery",),
                )

    if "POE2_SINGLE_SUPPORT_LIMIT" not in seen and not _SINGLE_SUPPORT_DEBUNK_RE.search(sentence):
        ss_m = _POE2_SINGLE_SUPPORT_RE.search(sentence)
        if ss_m is not None:
            _add(
                "POE2_SINGLE_SUPPORT_LIMIT", ss_m.group(0),
                "PoE1 allows duplicate support gems across different skill groups (single-support limit is PoE2 only).",
                "Remove the PoE2 single-support restriction.", ("support gem",),
            )


def _scan_sentence_legacy_and_fallacies(
    sentence: str, norm: str, zh_compact: str, seen: set[str], issues: list[ReplyIssue]
) -> None:
    if _NEGATION_EXEMPT_RE.search(sentence):
        return

    for prule in LEGACY_PATCH_TRAPS_POE1:
        if prule.rule_id not in _HARD_REMOVED_LEGACY_IDS or prule.rule_id in seen:
            continue
        if prule.rule_id == "PATCH_323_ALT_QUALITY_REMOVED":
            alt_m = _ALT_QUALITY_REPLY_RE.search(sentence)
            hit = alt_m.group(0).strip() if alt_m is not None else None
        else:
            hit = _match_rule_fields(
                sentence, norm, norm, zh_compact,
                en_exact=prule.en_exact, zh_exact=prule.zh_exact, en_words=prule.en_words,
                zh_phrases=prule.zh_phrases, regex=prule.regex,
                exclude_en_re=prule.exclude_en_re, exclude_zh_compounds=prule.exclude_zh_compounds,
            )
        if hit is not None:
            seen.add(prule.rule_id)
            issues.append(
                ReplyIssue(prule.rule_id, "legacy_removed", hit, sentence, prule.summary, prule.remediation, (hit,))
            )

    for rule_id, cat, term_re, trig_re, reason, remed, terms in _FALLACY_SPECS:
        if rule_id in seen:
            continue
        if term_re is not None and not term_re.search(sentence):
            continue
        m = trig_re.search(sentence)
        if m is not None:
            if (
                rule_id == "EXCEPTIONAL_SUPPORT_LEVEL_CAP"
                and _AWAKE_3_RE.search(sentence)
                and not _EXC_OVER_LV5_RE.search(sentence)
            ):
                continue
            seen.add(rule_id)
            issues.append(ReplyIssue(rule_id, cat, m.group(0), sentence, reason, remed, terms))


def scan_assistant_reply(text: str) -> list[ReplyIssue]:
    """Scan assistant final reply text for PoE2 leaks, 3.23-3.28 removals, and hard mechanic fallacies."""
    if not text or not text.strip():
        return []
    issues: list[ReplyIssue] = []
    seen: set[str] = set()
    for sentence in _split_sentences(text):
        norm = normalize_guardrail_text(sentence)
        zh_compact = normalize_zh_compact(sentence)
        _scan_sentence_poe2(sentence, norm, zh_compact, seen, issues)
        _scan_sentence_legacy_and_fallacies(sentence, norm, zh_compact, seen, issues)
    return issues


def _build_slot_alias_pattern() -> tuple[re.Pattern[str], dict[str, str]]:
    merged = {k.lower(): v for k, v in EQUIPMENT_SLOT_ALIASES.items() if len(k) >= 2}
    merged.update({k.lower(): v for k, v in _EXTRA_SLOT_ALIASES.items()})
    parts = [
        rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])" if k.isascii() else re.escape(k)
        for k in sorted(merged, key=len, reverse=True)
    ]
    return re.compile("|".join(parts), re.IGNORECASE), merged


_SLOT_ALIAS_RE, _SLOT_ALIAS_MAP = _build_slot_alias_pattern()


def extract_claimed_open_affix_slots(text: str) -> list[tuple[str, str]]:
    """Extract `[(normalized_slot, sentence), ...]` for positive open-affix / craftable slot claims."""
    if not text or not text.strip():
        return []
    results: list[tuple[str, str]] = []
    seen_slots: set[str] = set()
    for sentence in _split_sentences(text):
        if _OPEN_AFFIX_EXEMPT_RE.search(sentence):
            continue
        claims = list(_OPEN_AFFIX_CLAIM_RE.finditer(sentence))
        if not claims:
            continue
        for slot_m in _SLOT_ALIAS_RE.finditer(sentence):
            norm_slot = _SLOT_ALIAS_MAP.get(slot_m.group(0).lower()) or normalize_equipment_slot(slot_m.group(0))
            if not norm_slot or norm_slot in seen_slots:
                continue
            for cm in claims:
                dist = cm.start() - slot_m.end() if cm.start() >= slot_m.end() else slot_m.start() - cm.end()
                if 0 <= dist <= 25:
                    seen_slots.add(norm_slot)
                    results.append((norm_slot, sentence))
                    break
    return results


def detect_panel_or_delta_claims(text: str) -> list[str]:
    """Return concrete DPS/EHP panel or simulated delta claim snippets in assistant text."""
    cleaned = _strip_blockquotes(text)
    if not cleaned.strip():
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for pat in _PANEL_PATTERNS:
        for m in pat.finditer(cleaned):
            snippet = m.group(0).strip()
            key = snippet.lower()
            if snippet and key not in seen:
                seen.add(key)
                hits.append(snippet)
    return hits


def detect_trigger_breakpoint_claims(text: str) -> list[str]:
    """Return sentences asserting concrete trigger APS caps or CDR breakpoints."""
    if not text or not text.strip():
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for sentence in _split_sentences(text):
        if _TRIGGER_CTX_RE.search(sentence) and _TRIGGER_NUM_RE.search(sentence):
            snippet = sentence[:180]
            if snippet not in seen:
                seen.add(snippet)
                hits.append(snippet)
    return hits


def detect_build_source_in_text(text: str) -> tuple[bool, str | None]:
    """Detect whether text references a PoE1 build source and return `(has_source, source_hint)`."""
    if not text or not text.strip():
        return False, None
    for pat in _BUILD_SOURCE_PATTERNS:
        m = pat.search(text)
        if m is not None:
            return True, m.group(0).rstrip(".,);]")
    if _BUILD_BASE64_RE.search(text):
        return True, None
    return False, None
