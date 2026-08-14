"""SGCC 核心资产加载器."""
import os
import json
import lzma
import base64
from ..const import DOMAIN, LOGGER, RETRY_DELAY_MAP

class SgccAssetLoader:
    
    _cache = None
    _inv_sbox = None

    @classmethod
    def _get_inv_sbox(cls):

        if cls._inv_sbox is None:
            cls._inv_sbox = [0] * 256
            for i, v in enumerate(RETRY_DELAY_MAP):
                cls._inv_sbox[v] = i
        return cls._inv_sbox
    
    @classmethod
    def load_core(cls):
        if cls._cache:
            return cls._cache.get("SgccCoreLogic")
        current_dir = os.path.dirname(__file__)
        path = os.path.join(current_dir, "assets.dat")
        if not os.path.exists(path):
            LOGGER.error("【六壬推演】天机丢失：核心资产文件 %s 不存在", path)
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = f.read()
                raw_bytes = base64.b64decode(blob)
                outer_data = json.loads(lzma.decompress(raw_bytes).decode('utf-8'))
                logic_blob = outer_data.get("sgcc_logic")
                if not logic_blob:
                    LOGGER.error("【六壬推演】格式错误：资产包内未找到 sgcc_logic 模块")
                    return None
                inv_sbox = cls._get_inv_sbox()
                logic_bytes = base64.b64decode(logic_blob)
                recovered_bytes = bytes([inv_sbox[b] for b in logic_bytes])
                source_code = lzma.decompress(recovered_bytes[::-1]).decode('utf-8')
                namespace = {}
                exec(source_code, namespace)
                cls._cache = namespace
                core_class = namespace.get("SgccCoreLogic")
                if core_class:
                    LOGGER.info("【六壬推演】核心逻辑复活成功，格局已定")
                return core_class
        except Exception as e:
            LOGGER.error("【六壬推演】资产复活失败 | 细节: %s", e)
            return None