#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文案管理器 - 管理 TikTok 文案的增删改查
=======================================

文案数据存储在 captions.json 文件中。
每条文案包含：id、name（显示名称）、content（文案内容）、created_at、updated_at
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

SCRIPT_DIR = Path(__file__).parent.resolve()
CAPTIONS_FILE = SCRIPT_DIR / "captions.json"


def _load_captions() -> dict:
    """加载文案数据"""
    if CAPTIONS_FILE.exists():
        try:
            with open(CAPTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"captions": []}


def _save_captions(data: dict):
    """保存文案数据"""
    with open(CAPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_captions() -> List[Dict]:
    """
    获取所有文案列表
    
    Returns:
        List[Dict]: 文案列表，按创建时间倒序
    """
    data = _load_captions()
    captions = data.get("captions", [])
    # 按创建时间倒序排列
    captions.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return captions


def add_caption(name: str, content: str) -> Dict:
    """
    添加新文案
    
    Args:
        name: 文案显示名称（如"文案一"）
        content: 文案内容
        
    Returns:
        Dict: 新创建的文案对象
    """
    data = _load_captions()
    now = datetime.now().isoformat()
    
    caption = {
        "id": uuid.uuid4().hex[:12],
        "name": name.strip(),
        "content": content.strip(),
        "created_at": now,
        "updated_at": now,
    }
    
    data.setdefault("captions", []).append(caption)
    _save_captions(data)
    return caption


def update_caption(caption_id: str, name: str = None, content: str = None) -> Optional[Dict]:
    """
    更新文案
    
    Args:
        caption_id: 文案 ID
        name: 新名称（可选）
        content: 新内容（可选）
        
    Returns:
        Optional[Dict]: 更新后的文案，不存在返回 None
    """
    data = _load_captions()
    captions = data.get("captions", [])
    
    for caption in captions:
        if caption["id"] == caption_id:
            if name is not None:
                caption["name"] = name.strip()
            if content is not None:
                caption["content"] = content.strip()
            caption["updated_at"] = datetime.now().isoformat()
            _save_captions(data)
            return caption
    
    return None


def delete_caption(caption_id: str) -> bool:
    """
    删除文案
    
    Args:
        caption_id: 文案 ID
        
    Returns:
        bool: 是否删除成功
    """
    data = _load_captions()
    captions = data.get("captions", [])
    
    original_length = len(captions)
    data["captions"] = [c for c in captions if c["id"] != caption_id]
    
    if len(data["captions"]) < original_length:
        _save_captions(data)
        return True
    return False


def get_caption(caption_id: str) -> Optional[Dict]:
    """
    获取单条文案
    
    Args:
        caption_id: 文案 ID
        
    Returns:
        Optional[Dict]: 文案对象，不存在返回 None
    """
    data = _load_captions()
    for caption in data.get("captions", []):
        if caption["id"] == caption_id:
            return caption
    return None


def get_caption_text(caption_id: str) -> Optional[str]:
    """
    获取文案纯文本内容（用于发送）
    
    Args:
        caption_id: 文案 ID
        
    Returns:
        Optional[str]: 文案文本内容
    """
    caption = get_caption(caption_id)
    if caption:
        return caption.get("content", "")
    return None


# 初始化默认文案（如果文件为空）
def init_default_captions():
    """如果没有任何文案，创建默认示例文案"""
    data = _load_captions()
    if not data.get("captions"):
        add_caption(
            name="文案一",
            content="#fyp #viral #trending\n\nCheck out this amazing video! 🔥\nDon't forget to like and share! ❤️\n\nFollow for more!"
        )
        add_caption(
            name="文案二",
            content="#日常 #生活记录 #vlog\n\n今天分享一个超有趣的内容 ✨\n喜欢的记得点赞收藏哦～\n\n每天更新，不见不散！"
        )


if __name__ == "__main__":
    # 测试
    init_default_captions()
    caps = list_captions()
    print(f"现有 {len(caps)} 条文案:")
    for c in caps:
        print(f"  [{c['id']}] {c['name']}: {c['content'][:50]}...")
