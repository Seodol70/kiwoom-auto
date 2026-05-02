from __future__ import annotations
import os
import json
import logging
import threading
from datetime import datetime, time
from typing import Any, Dict, Optional

# 기존 config 모듈 임포트 (기본값으로 활용)
try:
    import config as _legacy_config
except ImportError:
    _legacy_config = None

logger = logging.getLogger(__name__)

class ConfigManager:
    """
    통합 설정 관리자 (Singleton)
    
    1. config.py 의 정적 설정을 기본값으로 로드
    2. params/adaptive_params.json 의 동적 설정을 덮어쓰기
    3. 실시간 리로드 및 타입 안전한 접근 지원
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ConfigManager, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        
        self._config_data: Dict[str, Any] = {}
        self._params_path = os.path.join("params", "adaptive_params.json")
        self._load_lock = threading.RLock()
        
        self.reload()
        self._initialized = True

    def reload(self):
        """설정 파일들을 다시 읽어 메모리에 적재한다."""
        with self._load_lock:
            new_data = {}
            
            # 1. config.py 에서 기본값 로드
            if _legacy_config:
                for attr in dir(_legacy_config):
                    if attr.isupper():
                        val = getattr(_legacy_config, attr)
                        if isinstance(val, dict):
                            new_data[attr] = val
                            for k, v in val.items():
                                new_data[k] = v
                        else:
                            new_data[attr] = val

            # 2. SmartScannerConfig 의 기본값들도 로드 (충돌 시 JSON이 덮어씀)
            try:
                from scanner.config import SmartScannerConfig
                for k, v in SmartScannerConfig.__dict__.items():
                    if not k.startswith("_") and not callable(v):
                        new_data[k] = v
            except ImportError:
                pass

            # 3. adaptive_params.json 에서 동적 파라미터 로드
            if os.path.exists(self._params_path):
                try:
                    with open(self._params_path, "r", encoding="utf-8") as f:
                        json_data = json.load(f)
                        if "params" in json_data:
                            for k, v in json_data["params"].items():
                                new_data[k] = v
                    logger.info("[ConfigManager] %s 로드 완료", self._params_path)
                except Exception as e:
                    logger.error("[ConfigManager] %s 로드 실패: %s", self._params_path, e)

            self._process_special_types(new_data)
            self._config_data = new_data

    def _process_special_types(self, data: Dict[str, Any]):
        """특정 필드들에 대해 타입 변환(시간 객체 등)을 수행한다."""
        for k, v in data.items():
            if "time" in k or "slot" in k or "open" in k or "close" in k:
                if isinstance(v, str) and ":" in v:
                    try:
                        parts = list(map(int, v.split(":")))
                        if len(parts) == 2:
                            data[k] = time(parts[0], parts[1])
                        elif len(parts) == 3:
                            data[k] = time(parts[0], parts[1], parts[2])
                    except:
                        pass

    def get(self, key: str, default: Any = None) -> Any:
        """설정값을 가져온다. (대소문자 구분 없음)"""
        # 1. 요청된 키 그대로 검색
        val = self._config_data.get(key)
        if val is not None:
            return val
        
        # 2. 대문자로 변환해서 검색 (config.py 호환)
        val = self._config_data.get(key.upper())
        if val is not None:
            return val
            
        return default

    def set_runtime(self, key: str, value: Any):
        """런타임 중에 설정을 일시적으로 변경한다 (파일 저장 안함)."""
        with self._load_lock:
            self._config_data[key] = value

    # 프로퍼티 방식으로 접근 지원
    def __getattr__(self, name: str) -> Any:
        if name in self._config_data:
            return self._config_data[name]
        
        upper_name = name.upper()
        if upper_name in self._config_data:
            return self._config_data[upper_name]
            
        raise AttributeError(f"'ConfigManager' object has no attribute '{name}'")

# 전역 인스턴스 생성
config_manager = ConfigManager()
