"""
新疆燃气 API 加密解密模块。

本模块实现新疆燃气接口所需的混合加密机制（Hybrid Encryption）：
1. RSA-1024：客户端生成密钥对，公钥发送给服务器；服务器用公钥加密 AES 密钥返回
2. AES-128-ECB：业务数据（登录、查询参数）使用 AES 加密后传输
3. 请求格式：{"data": Base64(密文), "key": Base64(RSA加密的AES密钥)}

加密流程说明：
- 握手阶段：客户端生成 RSA 密钥对，将公钥发送给服务器，服务器返回其公钥和 secret
- 请求阶段：每次请求时生成随机 AES 密钥，用 AES 加密业务数据，用服务器公钥加密 AES 密钥
- 响应阶段：服务器返回的数据同样用 AES 加密，密钥用客户端公钥加密，客户端用私钥解密

依赖：PyCryptodome (Crypto.Cipher.AES, Crypto.PublicKey.RSA)
"""
import logging      # 日志记录
import json         # JSON 序列化/反序列化
import base64       # Base64 编解码，用于密文传输
import secrets     # 密码学安全的随机数生成
import string      # 字符集（用于生成随机 AES 密钥）
from typing import Dict, Any, Optional      # 类型注解，用于函数参数与返回值
from Crypto.Cipher import AES, PKCS1_v1_5   # AES 对称加密；RSA PKCS#1 v1.5 填充
from Crypto.PublicKey import RSA            # RSA 密钥生成与导入
from Crypto.Util.Padding import pad, unpad  # PKCS7 填充与去填充

# 模块级日志记录器，用于输出加密解密过程中的调试和错误信息
_LOGGER = logging.getLogger(__name__)

class GasCrypto:
    """
    新疆燃气 API 加密解密工具类。
    
    提供 RSA 非对称加密（用于握手和密钥交换）和 AES 对称加密（用于业务数据传输）的封装。
    采用混合加密方案：RSA 用于安全交换 AES 密钥，AES 用于高效加密大量业务数据。
    """
    def __init__(self):
        """
        初始化加密工具类。
        
        初始化时会自动生成客户端 RSA 密钥对（1024 位），用于后续的密钥交换和数据解密。
        """
        self._client_key_pair = None  # 客户端 RSA 密钥对（含公钥和私钥），私钥用于解密服务器返回的 AES 密钥
        self._server_pub_key = None   # 服务端 RSA 公钥，用于加密每次请求时生成的 AES 密钥
        self._secret = None           # 握手时服务端返回的 Secret 字符串，部分接口需要携带此值
        self.generate_client_keys()

    @property
    def has_server_key(self) -> bool:
        """
        检查是否已获取服务端公钥。
        
        在调用 encrypt_payload 之前必须先通过握手接口获取并设置服务端公钥。
        
        Returns:
            bool: 若已设置服务端公钥返回 True，否则返回 False。
        """
        return self._server_pub_key is not None

    @property
    def secret(self) -> Optional[str]:
        """
        获取握手返回的 secret。
        
        此值在握手接口成功后由调用方设置，部分业务接口需要在请求中携带。
        
        Returns:
            Optional[str]: 握手返回的 secret 字符串，未设置时为 None。
        """
        return self._secret
    
    @secret.setter
    def secret(self, value: Optional[str]):
        """
        设置 secret 值。
        
        Args:
            value: 从握手接口响应中获取的 secret 字符串。
        """
        self._secret = value

    def generate_client_keys(self):
        """
        生成客户端 RSA 密钥对（2048 位）。
        
        使用 PyCryptodome 库的 RSA.generate() 生成，密钥对包含公钥和私钥。
        公钥用于发送给服务器，私钥用于解密服务器返回的加密数据。
        使用 2048 位以兼容服务器端加密需求，避免 "Message is larger than modulus" 错误。
        """
        try:
            self._client_key_pair = RSA.generate(2048)
        except Exception as e:
            _LOGGER.error("生成 RSA 密钥失败：%s", e)

    def get_client_public_key_clean(self) -> str:
        """
        获取清理后的客户端公钥（无头尾、无换行），用于发送给服务器。
        
        服务器接口要求公钥为纯 Base64 字符串，不能包含 PEM 格式的头尾标识
        （如 -----BEGIN PUBLIC KEY----- 和 -----END PUBLIC KEY-----）。
        
        Returns:
            str: 清理后的公钥字符串，仅包含 Base64 编码的密钥内容。
        """
        # 若密钥对不存在则重新生成
        if not self._client_key_pair:
            self.generate_client_keys()
        
        # 导出 PEM 格式公钥并解码为字符串
        client_pub_pem = self._client_key_pair.publickey().export_key().decode('utf-8')
        # 移除 PEM 头尾标识和换行符，得到纯 Base64 字符串
        return client_pub_pem.replace('-----BEGIN PUBLIC KEY-----', '') \
                             .replace('-----END PUBLIC KEY-----', '') \
                             .replace('\n', '')

    def set_server_public_key(self, server_key_str: str) -> bool:
        """
        设置服务端公钥。
        
        将握手接口返回的服务器公钥导入，用于后续加密请求时加密 AES 密钥。
        如果传入的公钥字符串缺少 PEM 头尾，会自动补充为标准 PEM 格式。
        
        Args:
            server_key_str: 服务端返回的公钥字符串，可为纯 Base64 或完整 PEM 格式。
            
        Returns:
            bool: 导入成功返回 True，失败返回 False。
        """
        if not server_key_str:
            return False
            
        try:
            # 判断是否为完整 PEM 格式，若否则自动补全头尾
            if "-----BEGIN PUBLIC KEY-----" not in server_key_str:
                pem_key = f"-----BEGIN PUBLIC KEY-----\n{server_key_str}\n-----END PUBLIC KEY-----"
            else:
                pem_key = server_key_str
            # 使用 PyCryptodome 解析并导入公钥
            self._server_pub_key = RSA.import_key(pem_key)
            return True
        except Exception as e:
            _LOGGER.error("导入服务器公钥失败: %s", e)
            return False

    def clear_server_key(self):
        """
        清除服务端公钥和 Secret。
        
        当遇到解密错误或密钥过期时调用，强制下次请求重新进行握手。
        """
        self._server_pub_key = None
        self._secret = None

    def encrypt_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        加密请求载荷（核心加密逻辑）。
        
        采用混合加密：每次请求生成新的随机 AES 密钥，保证前向安全性。
        加密流程：
        1. 动态生成一个 16 字节的随机 AES 密钥（AES-128 要求密钥长度为 16 字节）。
        2. 使用 AES-128-ECB 模式，用生成的密钥加密业务数据（payload）。
        3. 使用服务端 RSA 公钥，加密刚才生成的 AES 密钥。
        
        Args:
            payload: 需要加密的原始数据字典，将被序列化为 JSON 后加密。
            
        Returns:
            成功时返回格式为 {"data": "Base64密文", "key": "Base64(RSA加密的AES密钥)"} 的字典；
            失败时返回 None。
        """
        # 前置检查：必须先通过握手获取服务端公钥
        if not self._server_pub_key:
            _LOGGER.error("加密失败：缺少服务端公钥，请先握手")
            return None
            
        if not payload:
            _LOGGER.error("加密失败：数据字典为空")
            return None
            
        try:
            # ========== 步骤 1：生成随机 AES 密钥 ==========
            # 使用 secrets 模块生成密码学安全的随机字符串（字母+数字，共 16 字符 = 16 字节）
            # 字母+数字共 62 个字符，随机选 16 个作为 AES-128 密钥（16 字节）
            chars = string.ascii_letters + string.digits
            aes_key_str = ''.join(secrets.choice(chars) for _ in range(16))
            aes_key_bytes = aes_key_str.encode('utf-8')
            
            # ========== 步骤 2：AES 加密业务数据 ==========
            # 创建 AES-128-ECB 加密器（ECB 模式为接口要求，非推荐模式但需兼容）
            cipher_aes = AES.new(aes_key_bytes, AES.MODE_ECB)
            # 将字典序列化为紧凑 JSON 字符串（无多余空格）
            try:
                data_str = json.dumps(payload, separators=(',', ':'))
            except (TypeError, ValueError) as json_ex:
                _LOGGER.error("数据JSON序列化失败: %s", json_ex)
                return None
            # AES 块大小为 16 字节，需对数据进行 PKCS7 填充至块大小整数倍
            try:
                padded_data = pad(data_str.encode('utf-8'), AES.block_size)
            except ValueError as pad_ex:
                _LOGGER.error("数据填充失败: %s", pad_ex)
                return None
            # 执行 AES 加密，并将密文进行 Base64 编码便于传输
            encrypted_data = cipher_aes.encrypt(padded_data)
            encrypted_data_b64 = base64.b64encode(encrypted_data).decode('utf-8')
            
            # ========== 步骤 3：RSA 加密 AES 密钥 ==========
            # 使用 PKCS#1 v1.5 填充和服务器公钥加密 AES 密钥
            cipher_rsa = PKCS1_v1_5.new(self._server_pub_key)
            encrypted_key = cipher_rsa.encrypt(aes_key_bytes)
            encrypted_key_b64 = base64.b64encode(encrypted_key).decode('utf-8')
            
            # 返回符合接口要求的加密载荷格式
            return {
                "data": encrypted_data_b64,
                "key": encrypted_key_b64
            }
            
        except Exception as e:
            # 捕获加密过程中未预期的异常（如内存不足、库内部错误等）
            _LOGGER.error("加密过程发生未知错误: %s", e)
            return None

    def decrypt_payload(self, encrypted_response: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        解密响应载荷。
        
        服务器返回的加密响应格式与请求相同：{"data": "Base64密文", "key": "Base64(RSA加密的AES密钥)"}。
        其中 key 是服务器用客户端公钥加密的 AES 密钥，data 是用该 AES 密钥加密的业务数据。
        
        解密流程：
        1. 从响应中提取加密的 AES 密钥 (key) 和加密的数据 (data)。
        2. 使用客户端 RSA 私钥解密 AES 密钥。
        3. 使用解密出的 AES 密钥解密数据内容。
        4. 去除 PKCS7 填充并解析 JSON。
        
        Args:
            encrypted_response: 包含加密数据(data)和加密密钥(key)的响应字典。
            
        Returns:
            成功时返回解密并解析后的 JSON 数据字典；失败时返回 None。
        """
        # 前置检查：必须有客户端私钥才能解密服务器用公钥加密的 AES 密钥
        if not self._client_key_pair:
            _LOGGER.error("解密失败：缺少客户端私钥")
            return None
            
        if not encrypted_response or not isinstance(encrypted_response, dict):
            _LOGGER.error("解密失败：无效的加密响应数据")
            return None
            
        try:
            # 从响应中提取加密数据和加密密钥
            encrypted_data_b64 = encrypted_response.get('data')
            encrypted_key_b64 = encrypted_response.get('key')
            
            # 校验响应中必须包含 data 和 key 字段
            if not encrypted_data_b64 or not isinstance(encrypted_data_b64, str):
                _LOGGER.error("解密失败：缺少或无效的加密数据")
                return None
                
            if not encrypted_key_b64 or not isinstance(encrypted_key_b64, str):
                _LOGGER.error("解密失败：缺少或无效的加密密钥")
                return None
                
            # ========== 步骤 1：RSA 解密 AES 密钥 ==========
            # 使用客户端私钥和 PKCS#1 v1.5 解密
            cipher_rsa = PKCS1_v1_5.new(self._client_key_pair)
            try:
                encrypted_key_bytes = base64.b64decode(encrypted_key_b64)
            except Exception as b64_ex:
                _LOGGER.error("加密密钥Base64解码失败: %s", b64_ex)
                return None
            # PKCS1_v1_5.decrypt 的第二个参数为 sentinel：解密失败时返回该值；传 None 表示失败时抛出异常
            aes_key_bytes = cipher_rsa.decrypt(encrypted_key_bytes, None)
            
            if not aes_key_bytes:
                _LOGGER.error("RSA 解密失败，无法获取 AES 密钥")
                return None
                
            # ========== 步骤 2：AES 解密数据 ==========
            cipher_aes = AES.new(aes_key_bytes, AES.MODE_ECB)
            try:
                encrypted_data_bytes = base64.b64decode(encrypted_data_b64)
            except Exception as b64_ex:
                _LOGGER.error("加密数据Base64解码失败: %s", b64_ex)
                return None
            # 执行 AES-ECB 解密，得到带 PKCS7 填充的原始字节
            decrypted_data_bytes = cipher_aes.decrypt(encrypted_data_bytes)
            
            # ========== 步骤 3：去除 PKCS7 填充 ==========
            # 加密时使用了 pad，解密后需用 unpad 移除填充字节
            try:
                unpadded_data_bytes = unpad(decrypted_data_bytes, AES.block_size)
            except ValueError as unpad_ex:
                _LOGGER.error("数据去填充失败: %s", unpad_ex)
                return None
                
            # ========== 步骤 4：解析 JSON ==========
            try:
                data_str = unpadded_data_bytes.decode('utf-8')  # 将字节解码为 UTF-8 字符串
                return json.loads(data_str)  # 反序列化为 Python 字典
            except Exception as json_ex:
                _LOGGER.error("数据解析失败: %s", json_ex)
                return None
            
        except Exception as e:
            # 捕获解密过程中未预期的异常（如内存不足、库内部错误等）
            _LOGGER.error("解密过程发生未知错误: %s", e)
            return None
