"""研判输出的结构化 schema（strict `response_format`），不靠正则去解析自由文本。

每个字段都是被一次真实误判逼出来的（经过、内部文档）：

- `undistinguishable_candidates`：数据不够二选一时，逼它写出哪些分不开、还差什么数据
- `evidence.source_from`：标明引文来自 Zabbix 还是设备，`verify.py` 才能按来源逐字核
- `hypothesis_checklist`：固定 6 类方向逐项表态，自由列候选会漏掉「本端人为变更」
- `counter_evidence`：`ruled_out` 必须拿得出正面反证，「没看到」不算
- `contradiction`：`absence` 不是反证；再扫 claim 里的否定词，防止把 absence 硬标成 direct

跨字段的约束 strict schema 管不到，在 `check_business_rules` 里再校一遍。
"""

from __future__ import annotations

import copy
import re

#: 6 类固定的根因标准分类，覆盖网络故障诊断里常见的方向。**不是针对某一种
#: 故障specific 设计的**，接口 down、CPU 高、OSPF 邻居起不来这些不同类型的
#: 故障都能往这 6 类里对号入座，所以不随每次分析变化。
HYPOTHESIS_CATEGORIES = {
    "local_action": "本端有人/系统主动做了改动（配置变更、shutdown、重启、计划内维护）",
    "local_hardware_or_resource": "本端硬件或资源问题（物理层故障、CPU/内存耗尽、进程崩溃、设备重启）",
    "remote_or_upstream": "对端或上游的问题（对端设备/接口故障、上级网络路径问题）",
    "link_or_path_quality": "链路/路径本身的质量问题（丢包、延迟、协议不匹配，但两端接口未必是 down 的）",
    "management_plane_or_reachability": (
        "管不到它了，但它自己可能好好的（管理口 / 带外管理网 / 管理路径断）。"
        "**判据在邻居那里**：邻居看它的协议邻接还是 FULL、业务还在走，"
        "就说明断的是管理通道，不是设备宕机"
    ),
    "monitoring_or_collection_artifact": "监控采集本身的问题（采集器坏了、数据没采对、告警配置有问题——跟上一类的区别是这类影响的不止这一台）",
}

SOURCE_MONITOR = "监控"
SOURCE_DEVICE = "设备"

CONTRADICTION_DIRECT = "直接反证"
CONTRADICTION_ABSENCE = "未发现相关记录"

TIME_FAULT_WINDOW = "故障窗口"
TIME_CURRENT_SNAPSHOT = "当前快照"
TIME_INVARIANT = "与时间无关"

STATUS_SUPPORTED = "有证据支持"
STATUS_RULED_OUT = "已排除"
STATUS_CANNOT_DETERMINE = "暂时无法判断"
#: 给模型看的（英文）取值，见 `_HYPOTHESIS_ITEM_SCHEMA` 里那段说明
STATUS_WIRE = ("supported", "ruled_out", "cannot_determine")

ROLE_ROOT = "根因"
ROLE_CONSEQUENCE = "连带"
ROLE_INDEPENDENT = "无关"

WHO_AGENT_CAN_RETRY = "智能体可继续取证"
WHO_NEEDS_HUMAN = "需要人工取证"

_OLD_TO_CN_ENUM = {
    "zabbix": SOURCE_MONITOR,
    "device": SOURCE_DEVICE,
    "direct": CONTRADICTION_DIRECT,
    "absence": CONTRADICTION_ABSENCE,
    "fault_window": TIME_FAULT_WINDOW,
    "current_snapshot": TIME_CURRENT_SNAPSHOT,
    "time_invariant": TIME_INVARIANT,
    "supported": STATUS_SUPPORTED,
    "ruled_out": STATUS_RULED_OUT,
    "cannot_determine": STATUS_CANNOT_DETERMINE,
    "root": ROLE_ROOT,
    "consequence": ROLE_CONSEQUENCE,
    "independent": ROLE_INDEPENDENT,
    "agent_can_retry": WHO_AGENT_CAN_RETRY,
    "needs_human": WHO_NEEDS_HUMAN,
}

_CATEGORY_LABELS = {
    "local_action": "本端有人动过配置",
    "local_hardware_or_resource": "本机硬件或资源",
    "remote_or_upstream": "对端或上游",
    "link_or_path_quality": "链路本身或路径质量",
    "management_plane_or_reachability": "管理面或可达性",
    "monitoring_or_collection_artifact": "监控采集自身问题",
}


def normalize_statuses(parsed: dict | None) -> dict | None:
    """把模型吐的英文 status（supported/ruled_out/cannot_determine）转回中文，原地改。
    其余下游（校验、飞书卡、前端、落盘）继续只看中文。"""
    if isinstance(parsed, dict) and isinstance(parsed.get("hypothesis_checklist"), dict):
        for item in parsed["hypothesis_checklist"].values():
            if isinstance(item, dict) and isinstance(item.get("status"), str):
                item["status"] = normalize_enum(item["status"])
    return parsed


def normalize_enum(value: str) -> str:
    """旧记录里的英文 code 映射到中文；已经是中文或不认识的原样返回。"""
    return _OLD_TO_CN_ENUM.get(value, value)


_COUNTER_EVIDENCE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {
            "type": "string",
            "description": "这条反证否定了这个方向的哪一点",
        },
        "source": {
            "type": "string",
            "description": "从输入原文逐字复制的一段连续文本——跟 evidence.source 同样的逐字要求",
        },
        "source_from": {
            "type": "string",
            "enum": [SOURCE_MONITOR, SOURCE_DEVICE],
            "description": "这条 source 逐字摘自哪一份输入：监控侧数据还是设备侧数据",
        },
        "contradiction": {
            "type": "string",
            "enum": [CONTRADICTION_DIRECT, CONTRADICTION_ABSENCE],
            "description": (
                "直接反证=这条 source 的内容本身跟这个方向直接冲突（比如假设是"
                "\"对端关了接口\"，source 却是本端 administratively down）；"
                "未发现相关记录=这条 source 只是说明\"某类记录没有出现\"。"
                "**未发现相关记录不是反证，一句否定式的结论不能靠引用一条肯定句的"
                "原文来证明**——标这一档的这条不会被当作有效反证，"
                "对应方向也就不该标为已排除。"
            ),
        },
        "time_relevance": {
            "type": "string",
            "enum": [TIME_FAULT_WINDOW, TIME_CURRENT_SNAPSHOT, TIME_INVARIANT],
            "description": (
                "故障窗口=证据来自故障发生时刻附近（日志、历史监控值）；"
                "当前快照=这是现在查出来的状态（show 命令实时回显）；"
                "与时间无关=跟时间无关的事实（配置、硬件型号、MIB 定义）。"
                "对已经结束或时间线不清楚的故障，当前快照不能拿来直接排除"
                "故障当时的原因；如果唯一反证是当前快照，方向只能标为"
                "暂时无法判断，反证列表必须是空数组。"
            ),
        },
    },
    "required": ["claim", "source", "source_from", "contradiction", "time_relevance"],
    "additionalProperties": False,
}

#: **只收「没找到证据」一类的说法，不收「排除了」「并非」这类推理用词**——后者是拿正面证据
#: 排除方向，是对的。也没有改成「标了直接反证就不查」：直接反证是模型自己标的，会撒谎。
_NEGATION_MARKERS = (
    "没有发现",
    "没发现",
    "未发现",
    "未记录",
    "没有记录",
    "无记录",
    "找不到",
    "没找到",
    "没看到",
    "未看到",
    "未见",
    "未出现",
    "没有出现",
    "未检测到",
    "没有检测到",
    "不存在",
    "缺乏",
    "无相关",
    "没有任何",
)

_HYPOTHESIS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        # **这一项对模型必须用英文枚举**（出来后 `normalize_statuses()` 转回中文，下游不变）。
        # 真跑 + 重放实测：中文枚举下 qwen3.8-flash 六个方向全标同一个值
        # （5/5 全「已排除」，连它自己写「确认为根因」的那个方向也标已排除；换成别的中文名
        # 变成 4/4 全「有证据支持」），置信度被压成 low、还多出 2 条自相矛盾提示；
        # 换回英文枚举 supported/ruled_out/cannot_determine 4/4 判别正确、confidence high。
        "status": {
            "type": "string",
            "enum": list(STATUS_WIRE),
            "description": (
                "supported=现有证据支持这个方向（本次告警的根因所在的方向填这个）；"
                "ruled_out=现有证据里有正面反证足以排除这个方向（必须填 counter_evidence）；"
                "cannot_determine=没有支持也没有排除这个方向的证据——注意「没看到证据」"
                "本身不是反证，拿不出正面反证就只能填 cannot_determine，不能填 ruled_out"
            ),
        },
        "reason": {
            "type": "string",
            "description": "为什么是这个状态——排除要说排除依据，说不清要说缺什么数据",
        },
        "counter_evidence": {
            "type": "array",
            "description": (
                "标为已排除时必填非空：是哪条数据直接否定了这个方向，"
                "跟 evidence 一样要逐字引用、标来源。不是已排除时留空数组 []。"
                "拿不出来就别标已排除，改标暂时无法判断。"
            ),
            "items": _COUNTER_EVIDENCE_ITEM_SCHEMA,
        },
    },
    "required": ["status", "reason", "counter_evidence"],
    "additionalProperties": False,
}

def _hypothesis_checklist_properties(*, use_refs: bool) -> dict:
    if use_refs:
        return {
            category: {
                "$ref": "#/$defs/hypothesis_item",
                "description": description,
            }
            for category, description in HYPOTHESIS_CATEGORIES.items()
        }
    return {
        category: {
            **copy.deepcopy(_HYPOTHESIS_ITEM_SCHEMA),
            "description": description,
        }
        for category, description in HYPOTHESIS_CATEGORIES.items()
    }


def analysis_json_schema(*, use_refs: bool = True) -> dict:
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "fault_analysis",
            "strict": True,
            "schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": (
                        "**一句话定性，不超过 20 个字，卡片标题用。**"
                        "只说「是什么事」，例如「人为 shutdown 导致链路中断」"
                        "「对端设备重启」「上游丢包」。"
                        "不要复述 root_cause 整句，不要写成告警名字的同义词。\n"
                        "**尤其不要以设备名开头**——调用方会自己把设备名拼在前面，"
                        "你再写一遍就是同一个名字出现两次，白占标题栏。\n"
                        "反例（2026-09-24 实测，38 个字，开头重复了设备名）："
                        "「A1设备Ethernet0/1接口被人为执行shutdown操作导致链路中断」。\n"
                        "同样的意思写成：「人为 shutdown 导致链路中断」，14 个字。"
                        "判不出来的时候写「判不出根因，需人工介入」。"
                    ),
                },
                "root_cause": {
                    "type": "string",
                    "description": (
                        "一句话根因，必须到可处置粒度：看完这句话要知道该去找谁、"
                        "查什么、动什么。仅仅把告警名字换一种说法不算根因。如果数据"
                        "不足以判断到这个粒度，老实说判不出，不要为了显得自信硬给一个"
                        "武断结论。如果 hypothesis_checklist 里有两个及以上类别是"
                        "有证据支持或暂时无法判断，这里不要随便选一个当结论，"
                        "就写「现有数据无法区分 X 和 Y」，X/Y 必须对应 hypothesis_checklist 里"
                        "真实没被排除的类别，不能漏掉任何一个。\n"
                        "**但 X/Y 要写成中文人话，不许把 local_action、remote_or_upstream、"
                        "link_or_path_quality 这类字段名原样抄进来，也不许写"
                        "「见 undistinguishable_candidates」这种指向内部结构的话。**"
                        "这句是发到飞书和前端给网工看的，字段名对他毫无意义。"
                        "对照着写：local_action=本端有人动过配置，remote_or_upstream=对端或上游的问题，"
                        "link_or_path_quality=链路本身或路径质量，local_hardware_or_resource=本机硬件或资源，"
                        "management_plane_or_reachability=管理面或可达性，"
                        "monitoring_or_collection_artifact=监控采集自己的问题。"
                        "这里写完整因果，**短定性写在 headline 里，两者不要重复**。\n"
                        "**不许把 evidence 里的原文再抄一遍。** 证据链就排在这句话上面，"
                        "读的人刚看完。2026-09-24 真实反例：证据链已经列了 "
                        "`%LINK-5-CHANGED: ... administratively down` 和 "
                        "`%SYS-5-CONFIG_I: Configured from console by console` 两条回显，"
                        "根因那段又把这两条整句复制了一遍，同一件事读两遍。"
                        "这里只写因果推理——什么导致了什么、为什么能这么判——"
                        "要指证据就说「日志和配置保存时间吻合」，不要把日志原文搬过来。"
                    ),
                },
                "evidence": {
                    "type": "array",
                    "description": "支撑 root_cause 的证据链，每条都要能指回具体的原文",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {
                                "type": "string",
                                "description": "这条证据支持了什么结论",
                            },
                            "source": {
                                "type": "string",
                                "description": (
                                    "从输入原文逐字复制的一段连续文本（一行、一个指标"
                                    "名/值、一条日志）——必须是原文的精确子串，一个字都"
                                    "不能改，不能补全省略号、不能改标点、不能把两处不"
                                    "相邻的原文拼在一起、不能跨 Zabbix/设备两个来源拼接。"
                                    "复制不动就别引用这条证据。"
                                ),
                            },
                            "source_from": {
                                "type": "string",
                                "enum": [SOURCE_MONITOR, SOURCE_DEVICE],
                                "description": "这条 source 逐字摘自哪一份输入——监控侧数据还是设备侧数据",
                            },
                            "time_relevance": {
                                "type": "string",
                                "enum": [TIME_FAULT_WINDOW, TIME_CURRENT_SNAPSHOT, TIME_INVARIANT],
                                "description": (
                                    f"这条证据说的是哪个时刻。{TIME_FAULT_WINDOW}=故障发生前后那段时间的"
                                    f"历史或日志；{TIME_CURRENT_SNAPSHOT}=现在去查才查到的状态"
                                    "（show 命令的当前回显、取数时刻的接口状态都算）；"
                                    f"{TIME_INVARIANT}=跟时间无关的事实（设备型号、配置文本、拓扑关系）。"
                                    "**告警已经恢复时，当前快照说明不了故障发生时的情况**，"
                                    "别拿它当支撑根因的主要依据。"
                                ),
                            },
                        },
                        "required": ["claim", "source", "source_from", "time_relevance"],
                        "additionalProperties": False,
                    },
                },
                "ruled_out": {
                    "type": "array",
                    "description": "排除了哪些具体的可能（比 hypothesis_checklist 更细，比如某个具体命令/型号级别的猜测），以及凭什么排除的",
                    "items": {
                        "type": "object",
                        "properties": {
                            "possibility": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["possibility", "reason"],
                        "additionalProperties": False,
                    },
                },
                "hypothesis_checklist": {
                    "type": "object",
                    "description": (
                        "对 6 类标准根因方向逐项表态，**全部必填，不许跳过任何一类**——"
                        "哪怕你觉得某一类明显不相关，也要填已排除加一句为什么不相关，"
                        "不能因为觉得不重要就漏填。这是防止「没想到某个方向」的结构性保险。"
                    ),
                    "properties": _hypothesis_checklist_properties(use_refs=use_refs),
                    "required": list(HYPOTHESIS_CATEGORIES),
                    "additionalProperties": False,
                },
                "alert_roles": {
                    "type": "array",
                    "description": (
                        "每条候选告警在本次 incident 里的角色。每条候选告警必须恰好一项："
                        "根因=根因告警；连带=由其它候选告警引起的连带告警；"
                        "无关=跟其它候选不属于同一次故障。角色为连带时"
                        "caused_by_eventid 必须填本次候选里的 eventid；根因或无关时"
                        "caused_by_eventid 填空字符串。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "eventid": {"type": "string", "description": "候选告警的 eventid"},
                            "role": {
                                "type": "string",
                                "enum": [ROLE_ROOT, ROLE_CONSEQUENCE, ROLE_INDEPENDENT],
                                "description": "这条候选告警在本次分析中的角色",
                            },
                            "reason": {"type": "string", "description": "一句话：为什么是因/是果/是不相干"},
                            "caused_by_eventid": {
                                "type": "string",
                                "description": (
                                    "角色为连带时必填，指向引起它的本次候选 eventid；"
                                    "如果指不出来就不能填连带，只能填无关"
                                ),
                            },
                        },
                        "required": ["eventid", "role", "reason", "caused_by_eventid"],
                        "additionalProperties": False,
                    },
                },
                "grouping": {
                    "type": "array",
                    "description": (
                        "把候选告警分成若干个 incident。**一组 = 一次故障。**"
                        "所有候选 eventid 必须被覆盖且只能来自本次候选集合；"
                        "单告警场景也要填一组，events 里只有它自己。"
                        "**互不相关的告警必须拆成多组，不要塞进同一组。**"
                        "判断相关的依据：涉及同一个对象（接口、邻居、设备）、"
                        "时间上构成因果、或者在拓扑上相邻。"
                        "拿不准就拆开——多出一组最多是多发一张卡，"
                        "把不相关的合成一组会给出一个假的因果，那个错更贵。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "events": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "属于同一个 incident 的候选 eventid 列表",
                            },
                            "why_same": {
                                "type": "string",
                                "description": (
                                    "为什么这些告警是同一次故障。"
                                    "**如果你觉得它们其实不相关，就不要放在同一组**——"
                                    "拆成多组，别在这里写「暂不合并」之类的话。"
                                ),
                            },
                        },
                        "required": ["events", "why_same"],
                        "additionalProperties": False,
                    },
                },
                "undistinguishable_candidates": {
                    "type": "array",
                    "description": (
                        "把 hypothesis_checklist 里所有没被标为已排除的类别（有证据支持"
                        "或暂时无法判断）翻译成人话说清楚——**必填字段，没有歧义"
                        "就填空数组** `[]`。只要 hypothesis_checklist 里有两个及以上"
                        "类别没被标为已排除，这里就必须非空，且要覆盖到 checklist 里"
                        "每一个没被排除的类别，不能只挑一部分说。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidates": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "至少两个：现有证据同样支持、无法区分的候选根因（对应 hypothesis_checklist 里没被排除的类别）",
                            },
                            "why_indistinguishable": {
                                "type": "string",
                                "description": "为什么现有数据没法在这些候选之间二选一",
                            },
                            "what_data_would_help": {
                                "type": "string",
                                "description": "要拿到什么数据/证据才能区分开",
                            },
                            "how_to_get_it": {
                                "type": "string",
                                "description": (
                                    "**具体怎么拿到这份数据。** 能写成命令就写命令原文"
                                    "（登哪台、跑哪条 show，比如 "
                                    "`show logging | begin Sep 23 12:08`）；"
                                    "要去别的系统就说清去哪个系统、看什么。"
                                    "**不许写「进一步排查」「检查相关配置」「联系相关人员」"
                                    "这种正确的废话**——看完这句话，人要能立刻动手，"
                                    "不用再想一遍从哪下手。"
                                ),
                            },
                            "who": {
                                "type": "string",
                                "enum": [WHO_AGENT_CAN_RETRY, WHO_NEEDS_HUMAN],
                                "description": (
                                    "这份数据谁去拿。智能体可继续取证=只读工具够得着，"
                                    "再跑一轮就能拿到（没查的对端设备、没取的历史指标、"
                                    "这轮预算用完没查完的）；需要人工取证=工具够不着，"
                                    "得人去做（看物理线路、问变更单、登非网络设备、动配置）。"
                                    "**拿不准填需要人工取证**——让人白等一轮，比直说要人介入贵。"
                                ),
                            },
                        },
                        "required": ["candidates", "why_indistinguishable", "what_data_would_help",
                                     "how_to_get_it", "who"],
                        "additionalProperties": False,
                    },
                },
            },
                "related_alerts": {
                    "type": "array",
                    "description": (
                        "本次候选之外、**拓扑相邻设备上**的告警，你判断跟本次是同一件事的"
                        "（比如同一条链路的两端）。eventid 必须在输入原文里出现过（取证时"
                        "查到的），不许凭印象写；本次候选里的 eventid 不要放这里。没有就填 []。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "eventid": {"type": "string", "description": "对端告警的 eventid，原文里出现过的"},
                            "host": {"type": "string", "description": "对端设备名"},
                            "link": {"type": "string", "description": "两台之间是哪条线，如 D1 Ethernet1/0 ↔ A1 Ethernet0/1；不经直连就写经过谁"},
                            "reason": {"type": "string", "description": "一句话：凭什么认为是同一件事"},
                        },
                        "required": ["eventid", "host", "link", "reason"],
                        "additionalProperties": False,
                    },
                },
            "required": [
                "headline",
                "root_cause",
                "evidence",
                "ruled_out",
                "hypothesis_checklist",
                "alert_roles",
                "grouping",
                "undistinguishable_candidates",
                "related_alerts",
            ],
            "additionalProperties": False,
            },
        },
    }
    if use_refs:
        schema["json_schema"]["schema"]["$defs"] = {
            "hypothesis_item": copy.deepcopy(_HYPOTHESIS_ITEM_SCHEMA)
        }
    return schema


ANALYSIS_JSON_SCHEMA = analysis_json_schema(use_refs=True)


def derive_confidence(parsed: dict) -> str:
    """从 hypothesis_checklist 推置信度。返回 "high" / "medium" / "low"。"""
    checklist = parsed.get("hypothesis_checklist") or {}
    supported_count = 0
    non_ruled_out_count = 0

    for cat in HYPOTHESIS_CATEGORIES:
        status = normalize_enum((checklist.get(cat) or {}).get("status"))
        if status == STATUS_SUPPORTED:
            supported_count += 1
        if status != STATUS_RULED_OUT:
            non_ruled_out_count += 1

    if non_ruled_out_count == 0:
        confidence = "low"
    elif supported_count == 1 and non_ruled_out_count == 1:
        confidence = "high"
    elif supported_count == 1 and non_ruled_out_count >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    if parsed.get("undistinguishable_candidates") and confidence == "high":
        return "medium"
    # 一条可核查的证据引文都没有，就不给 high（取证被预算截断后只带出结论、没带出证据时会这样）。
    if confidence == "high" and not (parsed.get("evidence") or []):
        return "medium"
    return confidence


def _fault_is_probably_ended(source_text: str | None) -> bool:
    """故障是不是已经结束了（H1 时间相关性规则用）。看不出来就保守地返回 True：
    宁可误判一次长时间持续的故障，也不让现在的 show 快照去否定当时发生过的事。
    """
    if not source_text:
        return True

    lowered = source_text.lower()
    if any(marker in source_text for marker in ("恢复", "清除", "已恢复", "恢复时间", "恢复于")):
        return True
    if any(marker in lowered for marker in ("resolved", "recovered", "cleared", "recovery")):
        return True

    recovery_ids = re.findall(r'"[rc]_eventid"\s*:\s*"([1-9]\d*)"', source_text)
    if recovery_ids:
        return True

    epoch_clocks = [int(v) for v in re.findall(r'"clock"\s*:\s*"(\d{10})"', source_text)]
    if len(epoch_clocks) >= 2 and max(epoch_clocks) - min(epoch_clocks) > 15 * 60:
        return True

    clock_times = []
    for hour, minute, second in re.findall(r"\b(\d{2}):(\d{2}):(\d{2})\b", source_text):
        try:
            clock_times.append(int(hour) * 3600 + int(minute) * 60 + int(second))
        except ValueError:
            continue
    if len(clock_times) >= 2 and max(clock_times) - min(clock_times) > 15 * 60:
        return True

    looks_active = (
        '"r_eventid": "0"' in source_text
        or '"c_eventid": "0"' in source_text
        or "当前活跃告警" in source_text
        or "active problem" in lowered
    )
    if looks_active:
        return False

    return True


def check_business_rules(parsed: dict, source_text: str | None = None) -> list[str]:
    """strict schema 管不到的跨字段逻辑，在这里再校一遍。返回违反的规则，空表示没问题。"""
    violations = []
    candidates = parsed.get("undistinguishable_candidates") or []
    checklist = parsed.get("hypothesis_checklist") or {}
    fault_probably_ended = _fault_is_probably_ended(source_text)


    # **故障已经结束，却一条故障窗口的证据都没有。**
    #
    # 那次真实误判（V2-vios Gi0/3，OSPF + 3 条 BGP 同时 down、0 秒自愈）
    # 暴露出来的：原来 `time_relevance` 只有反证那边有，证据链这边压根没这个字段，
    # 规则看不见「这条是现在才查到的」。
    #
    # **这条只抓最糟的那种**：整条证据链没有一条来自故障窗口，全是当前快照和
    # 与时间无关的事实，却照样下了结论。「快照占比过半」「快照被用来支持误报结论」
    # 这些更细的毛病**故意不写成规则**——判定要靠读上下文，硬写成规则只会误伤，
    # 那是人看卡片时该管的事。
    evidence_list = parsed.get("evidence") or []
    if fault_probably_ended and evidence_list:
        windows = [e for e in evidence_list
                   if normalize_enum(e.get("time_relevance")) == TIME_FAULT_WINDOW]
        if not windows and any(e.get("time_relevance") for e in evidence_list):
            violations.append(
                f"故障已经恢复，但{len(evidence_list)}条证据里没有一条来自故障窗口，"
                "全是现在才查到的状态或跟时间无关的事实。"
                "当前状态说明不了故障发生时的情况——要么取故障窗口的历史和日志，"
                "要么照实说现在的数据不足以定性。"
            )

    non_ruled_out = [
        cat
        for cat in HYPOTHESIS_CATEGORIES
        if normalize_enum((checklist.get(cat) or {}).get("status")) != STATUS_RULED_OUT
    ]

    # **同一条规则在多个方向上犯，只说一次。**
    #
    # 真实反例（一条真实告警）：六个方向全标了已排除、一条反证都没给，
    # 卡片上就刷出六行一模一样的话，只有方向名不同。值班的人看到的是一堵墙。
    no_counter_evidence: list[str] = []

    for cat in HYPOTHESIS_CATEGORIES:
        item = checklist.get(cat) or {}
        label = _CATEGORY_LABELS[cat]
        status = normalize_enum(item.get("status"))
        ce_list = item.get("counter_evidence") or []
        if status == STATUS_RULED_OUT and not ce_list:
            no_counter_evidence.append(label)
        if status == STATUS_RULED_OUT:
            for i, ce in enumerate(ce_list):
                ordinal = i + 1
                contradiction = normalize_enum(ce.get("contradiction"))
                time_relevance = normalize_enum(ce.get("time_relevance"))
                if contradiction == CONTRADICTION_ABSENCE:
                    violations.append(
                        f"「{label}」这个方向的第 {ordinal} 条反证只是说没发现相关记录。"
                        "这不能当作反证；要排除这个方向，必须给出直接冲突的证据，"
                        "否则只能算暂时无法判断。"
                    )
                if (
                    contradiction == CONTRADICTION_DIRECT
                    and time_relevance == TIME_CURRENT_SNAPSHOT
                    and fault_probably_ended
                ):
                    violations.append(
                        f"「{label}」这个方向被排除，依据只是当前快照（现在查到的状态），不是故障发生时的记录，"
                        f"所以第 {ordinal} 条排除理由不算数（这个方向应算作「暂时无法判断」）。"
                    )
                claim = ce.get("claim") or ""
                if any(marker in claim for marker in _NEGATION_MARKERS):
                    violations.append(
                        f"「{label}」这个方向的第 {ordinal} 条反证里写了「{claim}」，"
                        "这类说法是在说没找到证据，不是直接反证。没找到不等于不存在，"
                        "不能据此排除这个方向。"
                    )

    if len(no_counter_evidence) >= len(HYPOTHESIS_CATEGORIES):
        # 全排掉了还一条反证没有，这不是某一条没写好，是整个排查没做
        violations.append(
            f"它把 {len(no_counter_evidence)} 个可能原因全标成已排除，却一条反证都没给。"
            "没有反证就不能排除——这等于排查过程是空的，结论没有依托。"
        )
    elif no_counter_evidence:
        violations.append(
            "它说这几个方向已排除，却没给出任何反证：" + "、".join(f"「{x}」" for x in no_counter_evidence)
            + "。没有反证就不能排除，只能算暂时无法判断。"
        )

    # **它给了结论，清单里却没有一个方向说得上「有证据支持」。**
    #
    # 那张真卡（一条真实告警）就是这样：根因写得明明白白
    # （历史旧名 A1-iol 的 Ethernet0/1 两次被 console 执行 shutdown，五条证据逐条带回显），
    # 但六个方向里一个「有证据支持」都没有——推导出来的置信度因此是 low，
    # 卡片上一边写着确凿的根因，一边说「分不出根因」，自己跟自己打架。
    #
    # 卡片那半已经按「它自己承认判不出没有」分支修好了，不会再自相矛盾。
    # **这条规则管的是另一半**：让这种不一致说出来，别静悄悄地过去。
    supported_count = sum(
        1 for cat in HYPOTHESIS_CATEGORIES
        if normalize_enum((checklist.get(cat) or {}).get("status")) == STATUS_SUPPORTED
    )
    # **前提是清单填全了。** schema 要求六个方向都表态，少一个就说明这份结论
    # 本身残缺（另有规则管），这时候再数"有几个有证据支持"没有意义。
    checklist_complete = all(cat in checklist for cat in HYPOTHESIS_CATEGORIES)
    # **还要真的给了根因。** 规则的话术是「它给出了根因，但……」，
    # 没有 root_cause 就没有这个"但"。
    has_root_cause = bool(str(parsed.get("root_cause") or "").strip())
    # 六个方向全标已排除又一条反证都没给的时候，上面那条已经把问题说透了，
    # 这条再说一遍「没有方向支持它」是同一件事换个说法，两条一起刷更难读。
    all_ruled_out_without_evidence = len(no_counter_evidence) >= len(HYPOTHESIS_CATEGORIES)
    if (has_root_cause and checklist_complete and not candidates
            and supported_count == 0 and not all_ruled_out_without_evidence):
        violations.append(
            "它给了根因，却没有任何一条排查结论支持它——"
            "它把所有可能原因要么标成已排除，要么标成说不准。"
            "这条根因可能是对的，但它没交代清楚依据；照着去核实，别当成已经查实。"
        )


    if len(non_ruled_out) >= 2 and not candidates:
        labels = "、".join(_CATEGORY_LABELS[cat] for cat in non_ruled_out)
        violations.append(
            f"还有 {len(non_ruled_out)} 个方向没有被排除：{labels}。"
            "既然这些方向还分不出来，就必须把候选根因和还需要的数据说清楚。"
        )
    if candidates and len(non_ruled_out) < 2:
        if non_ruled_out:
            labels = "、".join(_CATEGORY_LABELS[cat] for cat in non_ruled_out)
            detail = f"它其实只剩「{labels}」没排除"
        else:
            detail = "它其实把所有可能原因都排除了"
        violations.append(
            f"它说有几个原因现在分不出来，但{detail}。"
            "既然只剩一个（或一个不剩），就不该说分不出来。"
        )
    for i, candidate in enumerate(candidates):
        if not candidate.get("how_to_get_it") or not candidate.get("who"):
            violations.append(
                f"第 {i + 1} 组候选根因没有写清后续证据该怎么拿、由谁去拿。"
                "分不出根因时，必须给出可执行的下一步取证办法和负责人。"
            )

    alert_roles = parsed.get("alert_roles")
    grouping = parsed.get("grouping")
    if alert_roles is not None:
        candidate_eventids = {str(role.get("eventid", "")) for role in alert_roles if role.get("eventid")}
        for i, role in enumerate(alert_roles):
            role_name = normalize_enum(role.get("role"))
            ordinal = i + 1
            if role_name == ROLE_CONSEQUENCE:
                caused_by = str(role.get("caused_by_eventid") or "")
                if not caused_by:
                    violations.append(
                        f"第 {ordinal} 条告警被标成「连带」，却没指出是被哪一条引起的。"
                        "指不出来源就只能算无关。"
                    )
                elif caused_by not in candidate_eventids:
                    violations.append(
                        f"第 {ordinal} 条告警说自己是由 {caused_by} 引起的，"
                        "但这个编号不在本次候选告警里。连带关系只能指向本次一起分析的告警。"
                    )

        if grouping is not None:
            grouped_eventids = {
                str(eventid)
                for group in grouping
                for eventid in (group.get("events") or [])
                if str(eventid)
            }
            if grouped_eventids != candidate_eventids:
                missing = sorted(candidate_eventids - grouped_eventids)
                extra = sorted(grouped_eventids - candidate_eventids)
                violations.append(
                    "故障分组必须刚好覆盖本次所有候选告警。"
                    f"漏掉了 {missing}，多写了 {extra}。"
                )

    candidate_ids = {str(r.get("eventid", "")) for r in (alert_roles or []) if r.get("eventid")}
    for i, rel in enumerate(parsed.get("related_alerts") or []):
        eid = str(rel.get("eventid") or "")
        if eid in candidate_ids:
            violations.append(
                f"第 {i + 1} 条关联告警写的是 {eid}，但它本来就是本次候选告警。"
                "关联告警只放本次候选之外、取证时额外查到的告警。"
            )
        elif source_text is not None and (not eid or eid not in source_text):
            violations.append(
                f"第 {i + 1} 条关联告警写的是 {eid}，但输入原文里找不到这个编号。"
                "关联告警必须是取证时真实查到过的，不能凭印象补。"
            )

    return violations
