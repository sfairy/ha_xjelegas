"""
新疆电力 API 国密算法加密解密模块。

本模块实现国网 95598 开放平台要求的国密算法：
1. SM4 对称加密 (SM4Utils)：CBC 模式，用于请求体/响应体的加解密
2. SM2 椭圆曲线加密 (ECCUtils)：用于加密 keyCode 等敏感数据
3. SM3 哈希 (sm3_hash)：用于请求签名，输出 64 位十六进制
4. 辅助函数：str_to_bytes、bytes_to_hex、generate_random_string、各类 Padding

国网请求体加密流程：SM4 加密 JSON -> SM3 签名(密文+timestamp) -> SM2 加密 keyCode
"""
import random         # 生成随机数/随机字符串（keyCode、UUID、k 等）
import urllib.parse   # URL 编码，用于 str_to_bytes 处理特殊字符
import base64         # Base64 编解码，SM4 密文输出格式
import binascii       # 二进制与十六进制转换，KDF 中的计数器编码
from math import ceil # 向上取整，KDF 循环次数计算
import copy           # 深拷贝，CBC 模式中 IV 块传递

# ==========================================
# 辅助工具函数
# ==========================================

def str_to_bytes(input_str):
    """
    将字符串转换为字节数组列表。
    处理 URL 编码字符，将其转换为对应的字节值。
    """
    bytes_list = []
    for char in input_str:
        quoted = urllib.parse.quote(char)
        if len(quoted) == 1:
            bytes_list.append(ord(char))
        else:
            for hex_part in quoted.split('%')[1:]:
                bytes_list.append(int('0x' + hex_part, 16))
    return bytes_list

def bytes_to_hex(bytes_input):
    """
    将字节数组列表转换为十六进制字符串。
    每个字节转换为 2 位十六进制数。
    """
    hex_str = ''
    for byte in bytes_input:
        temp_hex = hex(byte)[2:]
        if len(temp_hex) == 1:
            temp_hex = '0' + temp_hex
        hex_str += temp_hex
    return hex_str

def string_to_hex(s):
    """
    字符串转十六进制字符串。
    先转字节数组，再转十六进制。
    """
    return '' if s == '' else bytes_to_hex(str_to_bytes(s))

# ==========================================
# 核心加密接口函数
# ==========================================

def sm2_encrypt(data, public_key):
    """
    SM2/ECC 公钥加密函数.
    
    用于加密敏感数据（如密码），使用服务端的公钥。
    
    Args:
        data (str): 待加密的明文数据。
        public_key (str): 服务端公钥（十六进制字符串）。
        
    Returns:
        str: 加密后的密文（十六进制字符串，以 '04' 开头，表示未压缩格式）。
    """
    data_hex = string_to_hex(data).encode()
    # 初始化 ECC 实例，mode=1 对应 C1C3C2 模式 (SM2 标准)
    ecc = ECCUtils(public_key=public_key, mode=1)
    encrypted_bytes = ecc.encrypt(data_hex)
    return '04' + encrypted_bytes.hex()

def generate_random_string(length=None, charset_len=None, mode=None):
    """
    生成随机字符串或 UUID.
    
    用于生成 keyCode (密钥) 或 requestId。
    
    Args:
        length (int, optional): 指定生成的字符串长度。
        charset_len (int, optional): 字符集长度。
        mode (int, optional): 模式 (1=纯数字, 其他=字母数字)。
        
    Returns:
        str: 随机字符串。
    """
    digits = '0123456789'
    chars = list(digits)
    if mode == 1:
        chars = list(digits)
    else:
        chars = list('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')
    
    result = []
    if charset_len is None:
        charset_len = len(chars)
        
    if length:
        # 生成指定长度的随机字符串
        for _ in range(length):
            result.append(chars[int(random.random() * charset_len)])
    else:
        # 生成 UUID 格式的字符串 (xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx)
        for i in range(36):
            if i in [8, 13, 18, 23]:
                result.append('-')
            elif i == 14:
                result.append('4')
            else:
                r = int(random.random() * 16)
                if i == 19:
                    # yxxx: 8, 9, A, or B
                    result.append(chars[3 & r | 8])
                else:
                    result.append(chars[r])
    return ''.join(result)

def sm4_encrypt(data, key):
    """
    SM4 对称加密函数 (CBC 模式).
    
    用于加密请求体数据。
    
    Args:
        data (str): 待加密的明文数据 (JSON 字符串)。
        key (str): 密钥 (通常是 keyCode)。
        
    Returns:
        str: 加密后的密文 (Base64 编码)。
    """
    data_bytes = data.encode('utf-8')
    key_bytes = key.encode('utf-8')
    # 初始化 SM4，使用 PKCS7 填充
    sm4 = SM4Utils(padding_mode=3)
    sm4.set_key(key_bytes, SM4_ENCRYPT)
    # 使用密钥的前8位和后8位拼接作为 IV (初始向量)
    iv = key_bytes[0:8] + key_bytes[-8:]
    encrypted = sm4.crypt_cbc(iv, data_bytes)
    return base64.b64encode(encrypted).decode()

def sm4_decrypt(data, key):
    """
    SM4 对称解密函数 (CBC 模式).
    
    用于解密响应体数据。
    
    Args:
        data (str): 待解密的密文 (Base64 编码)。
        key (str): 密钥 (通常是 keyCode)。
        
    Returns:
        str: 解密后的明文数据。
    """
    data_bytes = base64.b64decode(data.encode('utf-8'))
    key_bytes = key.encode('utf-8')
    sm4 = SM4Utils(padding_mode=3)
    sm4.set_key(key_bytes, SM4_DECRYPT)
    # 使用密钥的前8位和后8位拼接作为 IV
    iv = key_bytes[0:8] + key_bytes[-8:]
    decrypted = sm4.crypt_cbc(iv, data_bytes)
    return decrypted.decode()

def sm3_hash(data):
    """
    SM3 密码杂凑算法 (哈希函数).
    
    用于生成请求签名 (sign) 或校验和。
    SM3 输出长度为 256 位 (32 字节)，通常表示为 64 位十六进制字符串。
    
    Args:
        data (str): 输入数据。
        
    Returns:
        str: 哈希值 (十六进制字符串)。
    """
    data_bytes = data.encode('utf-8')
    data_list = bytes_to_list(data_bytes)
    return m_hash(data_list)

# ==========================================
# 基础算法实现 (Lambda 与 Helper)
# ==========================================

# 按位异或运算，用于块加密中的 XOR 操作
xor = lambda a, b: list(map(lambda x, y: x ^ y, a, b))
# 32 位无符号整数循环左移 n 位
rotl = lambda x, n: x << n & 4294967295 | x >> 32 - n & 4294967295
# 大端序：从字节数组读取 32 位无符号整数
get_uint32_be = lambda key_data: key_data[0] << 24 | key_data[1] << 16 | key_data[2] << 8 | key_data[3]
# 大端序：将 32 位整数拆分为 4 字节列表
put_uint32_be = lambda n: [n >> 24 & 255, n >> 16 & 255, n >> 8 & 255, n & 255]

# Padding 模式实现
# PKCS7 填充：填充 n 个值为 n 的字节，使总长度为 block 的整数倍
pkcs7_padding = lambda data, block=16: data + [16 - len(data) % block for _ in range(16 - len(data) % block)]
# 零填充：用 0x00 填充至 block 的整数倍
zero_padding = lambda data, block=16: data + [0 for _ in range(16 - len(data) % block)]
# PKCS7 去填充：根据最后一个字节的值移除填充
pkcs7_unpadding = lambda data: data[:-data[-1]]
# 零去填充：移除末尾的零字节（仅移除最后一个字节，适用于末尾为 0 的情况）
zero_unpadding = lambda data, i=1: data[:-i] if data[-i] == 0 else i + 1
# 字节列表转 bytes
list_to_bytes = lambda data: b''.join([bytes((x,)) for x in data])
# bytes 转字节列表
bytes_to_list = lambda data: [x for x in data]
# 生成指定长度的随机十六进制字符串
random_hex = lambda x: ''.join([random.choice('0123456789abcdef') for _ in range(x)])

def pboc_padding(data, block=16):
    """
    PBOC 填充模式。
    在数据末尾添加 0x80，然后用 0x00 填充至 block 的整数倍。
    用于银行卡等金融场景。
    """
    hex_str = data.hex().upper()
    hex_len = len(hex_str)
    block_hex_len = block * 2
    
    if hex_len % block_hex_len != 0:
        hex_str += '80'
    while len(hex_str) % block_hex_len != 0:
        hex_str += '00'
    return bytes_to_list(bytes.fromhex(hex_str))

def iso9797m2_padding(data, block=16):
    """
    ISO9797 M2 填充模式。
    在数据末尾添加 0x80，然后用 0x00 填充至 block 的整数倍。
    """
    hex_str = data.hex().upper()
    block_hex_len = block * 2
    hex_str += '80'
    while len(hex_str) % block_hex_len != 0:
        hex_str += '00'
    return bytes_to_list(bytes.fromhex(hex_str))

def pboc_unpadding(data):
    """
    PBOC 去填充。
    从末尾向前查找 0x80 并移除其后的所有填充字节。
    """
    if len(data) < 16:
        raise Exception('数据长度错误!')
    if len(data) == 16:
        pass
    else:
        while data[-1:] != [128]:
            data.pop()
        data.pop()
    return data

def iso9797m2_unpadding(data):
    """
    ISO9797 M2 去填充。
    从末尾向前查找 0x80 并移除其后的所有填充字节。
    """
    if len(data) <= 16:
        raise Exception('数据长度错误!')
    while data[-1:] != [128]:
        data.pop()
    data.pop()
    return data

# ==========================================
# SM2/ECC 椭圆曲线算法实现
# ==========================================

# SM2 推荐曲线参数
default_ecc_table = {
    'n': 'FFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123',
    'p': 'FFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF',
    'g': '32c4ae2c1f1981195f9904466a39c9948fe30bbff2660be1715a4589334c74c7bc3736a2f4f6779c59bdcee36b692153d0a9877cc62a474002df32e52139f0a0',
    'a': 'FFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC',
    'b': '28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93'
}

class ECCUtils:
    """
    ECC (Elliptic Curve Cryptography) 椭圆曲线加密类.
    主要用于实现 SM2 非对称加密算法。
    """
    def __init__(self, public_key, ecc_table=default_ecc_table, mode=1, asn1=False):
        """
        初始化 ECC 实例.
        
        Args:
            public_key: 公钥 (Hex 字符串).
            ecc_table: 椭圆曲线参数 (默认 SM2).
            mode: 输出模式, 0=C1C2C3, 1=C1C3C2 (SM2 标准).
            asn1: 是否使用 ASN.1 编码 (默认 False).
        """
        self.public_key = public_key
        # 去掉 '04' 前缀 (未压缩标识)
        if public_key.startswith('04') and len(public_key) == 130:
            self.public_key = public_key[2:]
            
        self.ecc_table = ecc_table
        self.para_len = len(ecc_table['n'])
        # 计算 a + 3 (mod p) 用于优化计算
        self.ecc_a3 = (int(ecc_table['a'], base=16) + 3) % int(ecc_table['p'], base=16)
        
        assert mode in (0, 1), 'mode must be one of (0, 1)'
        self.mode = mode
        self.asn1 = asn1

    def _kg(self, k, Point):
        """
        椭圆曲线标量乘法：计算 k * Point。
        使用二进制展开法（从左到右扫描 k 的每一位）。
        """
        point_str = Point
        point_str = '%s%s' % (point_str, '1')  # 转为雅可比坐标 (x, y, 1)
        
        # 构造掩码 '800...0'
        mask_str = '8' + '0' * (self.para_len - 1)
        mask = int(mask_str, 16)
        
        Q = point_str
        is_first_bit = False
        
        # 遍历 k 的每一位
        for _ in range(self.para_len * 4):
            if is_first_bit:
                Q = self._double_point(Q)
            
            if k & mask != 0:
                if is_first_bit:
                    Q = self._add_point(Q, point_str)
                else:
                    is_first_bit = True
                    Q = point_str
            k = k << 1
            
        return self._convert_jacb_to_nor(Q)

    def _double_point(self, Point):
        """点倍积运算 (Jacobian 坐标)."""
        point_hex = Point
        len_point = len(point_hex)
        len_coord_pair = 2 * self.para_len
        
        if len_point < self.para_len * 2:
            return None
        
        x1 = int(point_hex[0:self.para_len], 16)
        y1 = int(point_hex[self.para_len:len_coord_pair], 16)
        
        if len_point == len_coord_pair:
            z1 = 1
        else:
            z1 = int(point_hex[len_coord_pair:], 16)
            
        p_int = int(self.ecc_table['p'], base=16)
        
        T1 = z1 * z1 % p_int
        T2 = y1 * y1 % p_int
        T3 = (x1 + T1) % p_int
        T4 = (x1 - T1) % p_int
        T5 = T3 * T4 % p_int
        T6 = y1 * z1 % p_int
        T7 = T2 * 8 % p_int
        T8 = x1 * T7 % p_int
        T5 = T5 * 3 % p_int
        
        # 优化：a = p - 3 时，M = 3*x1^2 + a*z1^4 = 3*(x1+z1^2)*(x1-z1^2)
        # T1 = z1^2, T1^2 = z1^4, T1 = ecc_a3 * z1^4
        T1 = T1 * T1 % p_int
        T1 = self.ecc_a3 * T1 % p_int
        T5 = (T5 + T1) % p_int  # M
        
        z3 = (T6 + T6) % p_int
        T1 = T5 * T5 % p_int
        T2 = T2 * T7 % p_int
        x3 = (T1 - T8) % p_int
        
        if T8 % 2 == 1:
            T4 = (T8 + (T8 + p_int >> 1) - T1) % p_int
        else:
            T4 = (T8 + (T8 >> 1) - T1) % p_int
            
        T5 = T5 * T4 % p_int
        y3 = (T5 - T2) % p_int
        
        fmt = '%%0%dx' % self.para_len
        fmt = fmt * 3
        return fmt % (x3, y3, z3)

    def _add_point(self, P1, P2):
        """点加运算 (Jacobian 坐标)."""
        len_coord_pair = 2 * self.para_len
        len_p1 = len(P1)
        len_p2 = len(P2)
        
        if len_p1 < len_coord_pair or len_p2 < len_coord_pair:
            return None
            
        x1 = int(P1[0:self.para_len], 16)
        y1 = int(P1[self.para_len:len_coord_pair], 16)
        if len_p1 == len_coord_pair:
            z1 = 1
        else:
            z1 = int(P1[len_coord_pair:], 16)
            
        x2 = int(P2[0:self.para_len], 16)
        y2 = int(P2[self.para_len:len_coord_pair], 16)
        
        p_int = int(self.ecc_table['p'], base=16)
        
        T1 = z1 * z1 % p_int
        T2 = y2 * z1 % p_int
        T3 = x2 * T1 % p_int
        T1 = T1 * T2 % p_int
        T2 = (T3 - x1) % p_int
        T3 = (T3 + x1) % p_int
        T4 = T2 * T2 % p_int
        T1 = (T1 - y1) % p_int
        T5 = z1 * T2 % p_int
        T2 = T2 * T4 % p_int
        T3 = T3 * T4 % p_int
        T6 = T1 * T1 % p_int
        T4 = x1 * T4 % p_int
        T7 = (T6 - T3) % p_int
        T2 = y1 * T2 % p_int
        T3 = (T4 - T7) % p_int
        T1 = T1 * T3 % p_int
        y3 = (T1 - T2) % p_int
        
        fmt = '%%0%dx' % self.para_len
        fmt = fmt * 3
        return fmt % (T7, y3, T5)

    def _convert_jacb_to_nor(self, Point):
        """雅可比坐标转仿射坐标 (x, y, z) -> (x/z^2, y/z^3)."""
        len_coord_pair = 2 * self.para_len
        x = int(Point[0:self.para_len], 16)
        y = int(Point[self.para_len:len_coord_pair], 16)
        z = int(Point[len_coord_pair:], 16)
        
        p_int = int(self.ecc_table['p'], base=16)
        
        z_inv = pow(z, p_int - 2, p_int) # z^-1
        z_inv_sq = z_inv * z_inv % p_int # z^-2
        z_inv_cube = z_inv_sq * z_inv % p_int # z^-3
        
        x_norm = x * z_inv_sq % p_int
        y_norm = y * z_inv_cube % p_int
        z_norm = z * z_inv % p_int # Should be 1
        
        if z_norm == 1:
            fmt = '%%0%dx' % self.para_len
            fmt = fmt * 2
            return fmt % (x_norm, y_norm)
        else:
            return None

    def encrypt(self, data):
        """
        加密数据.
        
        Args:
            data: 待加密数据的 Hex 字节串.
            
        Returns:
            bytes: 加密后的字节串.
        """
        fmt = '%s%s%s'
        data_hex = data.hex()
        
        # 1. 生成随机数 k
        k_hex = random_hex(self.para_len)
        k_int = int(k_hex, 16)
        
        # 2. 计算 C1 = [k]G = (x1, y1)
        C1 = self._kg(k_int, self.ecc_table['g'])
        
        # 3. 计算 [k]PB = (x2, y2)
        kP = self._kg(k_int, self.public_key)
        x2 = kP[0:self.para_len]
        y2 = kP[self.para_len:2 * self.para_len]
        
        msg_len = len(data_hex)
        
        # 4. 计算 t = KDF(x2 || y2, klen)，klen 为明文字节长度
        t = m_kdf(kP.encode('utf8'), msg_len // 2)
        
        if int(t, 16) == 0:
            return None # 极其罕见的情况，需要重新生成 k (这里简化处理)
        else:
            # 5. 计算 C2 = M ^ t
            fmt_c2 = '%%0%dx' % msg_len
            C2 = fmt_c2 % (int(data_hex, 16) ^ int(t, 16))
            
            # 6. 计算 C3 = Hash(x2 || M || y2)
            hash_input = bytes.fromhex(fmt % (x2, data_hex, y2))
            C3 = m_hash(bytes_to_list(hash_input))
            
            if self.mode == 1:
                # C1 C3 C2 (SM2 标准)
                return bytes.fromhex(fmt % (C1, C3, C2))
            else:
                # C1 C2 C3 (旧标准)
                return bytes.fromhex(fmt % (C1, C2, C3))

# ==========================================
# SM3 密码杂凑算法实现
# ==========================================

# SM3 初始向量 IV（8 个 32 位字）
IV = [1937774191, 1226093241, 388252375, 3666478592, 2842636476, 372324522, 3817729613, 2969243214]
# SM3 常量 T_j：前 16 轮用 0x79CC4519，后 48 轮用 0x7A879D8A
T_j = [2043430169] * 16 + [2055708042] * 48


def m_ff_j(x, y, z, j):
    """
    SM3 布尔函数 FF_j。
    0<=j<16: FF(X,Y,Z) = X ^ Y ^ Z
    16<=j<64: FF(X,Y,Z) = (X & Y) | (X & Z) | (Y & Z)
    """
    if 0 <= j and j < 16:
        return x ^ y ^ z
    elif 16 <= j and j < 64:
        return x & y | x & z | y & z
    return 0


def m_gg_j(x, y, z, j):
    """
    SM3 布尔函数 GG_j。
    0<=j<16: GG(X,Y,Z) = X ^ Y ^ Z
    16<=j<64: GG(X,Y,Z) = (X & Y) | (~X & Z)
    """
    if 0 <= j and j < 16:
        return x ^ y ^ z
    elif 16 <= j and j < 64:
        return x & y | ~x & z
    return 0


def m_p_0(x):
    """SM3 置换函数 P0：P0(X) = X ^ (X<<<9) ^ (X<<<17)"""
    return x ^ rotl(x, 9 % 32) ^ rotl(x, 17 % 32)


def m_p_1(x):
    """SM3 置换函数 P1：P1(X) = X ^ (X<<<15) ^ (X<<<23)"""
    return x ^ rotl(x, 15 % 32) ^ rotl(x, 23 % 32)

def m_cf(v_i, b_i):
    """
    SM3 压缩函数。
    对 64 字节的输入块 b_i 进行压缩，更新状态 v_i。
    """
    W = []
    # 消息扩展：将 16 个字扩展为 68 个字
    for j in range(16):
        val = 0
        base = 16777216
        for k in range(j * 4, (j + 1) * 4):
            val = val + b_i[k] * base
            base = int(base / 256)
        W.append(val)
        
    for j in range(16, 68):
        W.append(0)
        # W[j] = P1(W[j-16] ^ W[j-9] ^ (W[j-3] <<< 15)) ^ (W[j-13] <<< 7) ^ W[j-6]
        W[j] = m_p_1(W[j - 16] ^ W[j - 9] ^ rotl(W[j - 3], 15 % 32)) ^ rotl(W[j - 13], 7 % 32) ^ W[j - 6]
        
    W1 = []
    for j in range(0, 64):
        W1.append(0)
        W1[j] = W[j] ^ W[j + 4]
        
    A, B, C, D, E, F, G, H = v_i
    
    for j in range(0, 64):
        SS1 = rotl(rotl(A, 12 % 32) + E + rotl(T_j[j], j % 32) & 4294967295, 7 % 32)
        SS2 = SS1 ^ rotl(A, 12 % 32)
        TT1 = m_ff_j(A, B, C, j) + D + SS2 + W1[j] & 4294967295
        TT2 = m_gg_j(E, F, G, j) + H + SS1 + W[j] & 4294967295
        D = C
        C = rotl(B, 9 % 32)
        B = A
        A = TT1
        H = G
        G = rotl(F, 19 % 32)
        F = E
        E = m_p_0(TT2)
        
        # 保持 32 位无符号整数
        A, B, C, D, E, F, G, H = map(lambda x: x & 4294967295, [A, B, C, D, E, F, G, H])
        
    return [A ^ v_i[0], B ^ v_i[1], C ^ v_i[2], D ^ v_i[3], E ^ v_i[4], F ^ v_i[5], G ^ v_i[6], H ^ v_i[7]]

def m_hash(msg):
    """
    SM3 哈希运算。
    对消息进行填充后，按 64 字节分组迭代压缩，输出 256 位哈希值（64 位十六进制）。
    """
    padded_msg = msg
    msg_len = len(padded_msg)
    reserve_len = msg_len % 64
    
    padded_msg.append(128) # 0x80
    reserve_len = reserve_len + 1
    
    padding_len = 56
    if reserve_len > padding_len:
        padding_len = padding_len + 64
        
    for _ in range(reserve_len, padding_len):
        padded_msg.append(0)
        
    bit_len = msg_len * 8
    len_bytes = [bit_len % 256]
    for _ in range(7):
        bit_len = int(bit_len / 256)
        len_bytes.append(bit_len % 256)
        
    for i in range(8):
        padded_msg.append(len_bytes[7 - i])
        
    group_count = round(len(padded_msg) / 64)
    groups = []
    for i in range(0, group_count):
        groups.append(padded_msg[i * 64:(i + 1) * 64])
        
    v_states = []
    v_states.append(IV)
    for i in range(0, group_count):
        v_states.append(m_cf(v_states[i], groups[i]))
        
    final_state = v_states[len(v_states) - 1]
    result = ''
    for val in final_state:
        result = '%s%08x' % (result, val)
    return result

def m_kdf(z, klen):
    """
    密钥派生函数 KDF。
    基于 SM3 哈希，从共享秘密 z 派生出 klen 字节的密钥。
    输入 z 为 UTF-8 编码的十六进制字符串对应的 bytes。
    """
    klen_int = int(klen)
    counter = 1
    loop_count = ceil(klen_int / 32)
    z_bytes = [x for x in bytes.fromhex(z.decode('utf8'))]
    result = ''
    
    for _ in range(loop_count):
        # Hash(Z || counter)
        ct_bytes = [x for x in binascii.a2b_hex(('%08x' % counter).encode('utf8'))]
        hash_input = z_bytes + ct_bytes
        result = result + m_hash(hash_input)
        counter += 1
        
    return result[0:klen_int * 2]

# ==========================================
# SM4 对称加密算法实现
# ==========================================

# SM4 S 盒（字节替换表）
SM4_BOXES_TABLE = [214, 144, 233, 254, 204, 225, 61, 183, 22, 182, 20, 194, 40, 251, 44, 5, 43, 103, 154, 118, 42, 190, 4, 195, 170, 68, 19, 38, 73, 134, 6, 153, 156, 66, 80, 244, 145, 239, 152, 122, 51, 84, 11, 67, 237, 207, 172, 98, 228, 179, 28, 169, 201, 8, 232, 149, 128, 223, 148, 250, 117, 143, 63, 166, 71, 7, 167, 252, 243, 115, 23, 186, 131, 89, 60, 25, 230, 133, 79, 168, 104, 107, 129, 178, 113, 100, 218, 139, 248, 235, 15, 75, 112, 86, 157, 53, 30, 36, 14, 94, 99, 88, 209, 162, 37, 34, 124, 59, 1, 33, 120, 135, 212, 0, 70, 87, 159, 211, 39, 82, 76, 54, 2, 231, 160, 196, 200, 158, 234, 191, 138, 210, 64, 199, 56, 181, 163, 247, 242, 206, 249, 97, 21, 161, 224, 174, 93, 164, 155, 52, 26, 85, 173, 147, 50, 48, 245, 140, 177, 227, 29, 246, 226, 46, 130, 102, 202, 96, 192, 41, 35, 171, 13, 83, 78, 111, 213, 219, 55, 69, 222, 253, 142, 47, 3, 255, 106, 114, 109, 108, 91, 81, 141, 27, 175, 146, 187, 221, 188, 127, 17, 217, 92, 65, 31, 16, 90, 216, 10, 193, 49, 136, 165, 205, 123, 189, 45, 116, 208, 18, 184, 229, 180, 176, 137, 105, 151, 74, 12, 150, 119, 126, 101, 185, 241, 9, 197, 110, 198, 132, 24, 240, 125, 236, 58, 220, 77, 32, 121, 238, 95, 62, 215, 203, 57, 72]
# SM4 系统参数 FK（用于密钥扩展）
SM4_FK = [2746333894, 1453994832, 1736282519, 2993693404]
# SM4 固定参数 CK（用于密钥扩展）
SM4_CK = [462357, 472066609, 943670861, 1415275113, 1886879365, 2358483617, 2830087869, 3301692121, 3773296373, 4228057617, 404694573, 876298825, 1347903077, 1819507329, 2291111581, 2762715833, 3234320085, 3705924337, 4177462797, 337322537, 808926789, 1280531041, 1752135293, 2223739545, 2695343797, 3166948049, 3638552301, 4110090761, 269950501, 741554753, 1213159005, 1684763257]
# 加密/解密模式
SM4_ENCRYPT = 0
SM4_DECRYPT = 1
# 填充模式
NoPadding = 0
ZERO = 1
ISO9797M2 = 2
PKCS7 = 3
PBOC = 4


class SM4Utils:
    """
    SM4 对称加密算法类。
    支持 ECB、CBC 模式，支持多种填充方式（PKCS7、零填充、ISO9797M2、PBOC）。
    """
    def __init__(self, mode=SM4_ENCRYPT, padding_mode=PKCS7):
        """
        初始化 SM4 实例。
        Args:
            mode: 加密(0)或解密(1)模式
            padding_mode: 填充模式（NoPadding/ZERO/ISO9797M2/PKCS7/PBOC）
        """
        self.sk = [0] * 32  # 轮密钥，32 个 32 位字
        self.mode = mode
        self.padding_mode = padding_mode

    @classmethod
    def _round_key(cls, ka):
        """SM4 密钥扩展：从 32 位输入 ka 生成轮密钥."""
        buf = [0, 0, 0, 0]
        ka_bytes = put_uint32_be(ka)
        buf[0] = SM4_BOXES_TABLE[ka_bytes[0]]
        buf[1] = SM4_BOXES_TABLE[ka_bytes[1]]
        buf[2] = SM4_BOXES_TABLE[ka_bytes[2]]
        buf[3] = SM4_BOXES_TABLE[ka_bytes[3]]
        val = get_uint32_be(buf[0:4])
        return val ^ rotl(val, 13) ^ rotl(val, 23)

    @classmethod
    def _f(cls, x0, x1, x2, x3, rk):
        """SM4 轮函数 F：F(X0,X1,X2,X3,RK) = X0 ^ T(X1^X2^X3^RK)."""
        def sub_func(ka):
            buf = [0, 0, 0, 0]
            ka_bytes = put_uint32_be(ka)
            buf[0] = SM4_BOXES_TABLE[ka_bytes[0]]
            buf[1] = SM4_BOXES_TABLE[ka_bytes[1]]
            buf[2] = SM4_BOXES_TABLE[ka_bytes[2]]
            buf[3] = SM4_BOXES_TABLE[ka_bytes[3]]
            val = get_uint32_be(buf[0:4])
            return val ^ rotl(val, 2) ^ rotl(val, 10) ^ rotl(val, 18) ^ rotl(val, 24)
            
        return x0 ^ sub_func(x1 ^ x2 ^ x3 ^ rk)

    def set_key(self, key, mode):
        """
        设置密钥并生成 32 轮轮密钥。
        解密时轮密钥顺序与加密相反。
        """
        key_bytes = bytes_to_list(key)
        mk = [0, 0, 0, 0]
        k = [0] * 36
        mk[0] = get_uint32_be(key_bytes[0:4])
        mk[1] = get_uint32_be(key_bytes[4:8])
        mk[2] = get_uint32_be(key_bytes[8:12])
        mk[3] = get_uint32_be(key_bytes[12:16])
        
        k[0:4] = xor(mk[0:4], SM4_FK[0:4])
        for i in range(32):
            k[i + 4] = k[i] ^ self._round_key(k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ SM4_CK[i])
            self.sk[i] = k[i + 4]
            
        self.mode = mode
        if mode == SM4_DECRYPT:
            for i in range(16):
                temp = self.sk[i]
                self.sk[i] = self.sk[31 - i]
                self.sk[31 - i] = temp

    def one_round(self, sk, in_put):
        """对 16 字节输入块执行 32 轮 SM4 加密/解密."""
        out_bytes = []
        x = [0] * 36
        x[0] = get_uint32_be(in_put[0:4])
        x[1] = get_uint32_be(in_put[4:8])
        x[2] = get_uint32_be(in_put[8:12])
        x[3] = get_uint32_be(in_put[12:16])
        
        for i in range(32):
            x[i + 4] = self._f(x[i], x[i + 1], x[i + 2], x[i + 3], sk[i])
            
        out_bytes += put_uint32_be(x[35])
        out_bytes += put_uint32_be(x[34])
        out_bytes += put_uint32_be(x[33])
        out_bytes += put_uint32_be(x[32])
        return out_bytes

    def crypt_ecb(self, input_data):
        """
        ECB 模式加密/解密。
        每个块独立处理，无反馈。
        """
        data_bytes = input_data
        if self.mode == SM4_ENCRYPT:
            if self.padding_mode == NoPadding:
                pass
            if self.padding_mode == ZERO:
                data_bytes = zero_padding(bytes_to_list(data_bytes))
            if self.padding_mode == ISO9797M2:
                data_bytes = iso9797m2_padding(data_bytes)
            if self.padding_mode == PKCS7:
                data_bytes = pkcs7_padding(bytes_to_list(data_bytes))
            if self.padding_mode == PBOC:
                data_bytes = pboc_padding(data_bytes)
                
        length = len(data_bytes)
        offset = 0
        result = []
        while length > 0:
            result += self.one_round(self.sk, data_bytes[offset:offset + 16])
            offset += 16
            length -= 16
            
        if self.mode == SM4_DECRYPT:
            if self.padding_mode == NoPadding:
                pass
            if self.padding_mode == ZERO:
                return list_to_bytes(zero_unpadding(result))
            if self.padding_mode == ISO9797M2:
                return list_to_bytes(iso9797m2_unpadding(result))
            if self.padding_mode == PKCS7:
                return list_to_bytes(pkcs7_unpadding(result))
            if self.padding_mode == PBOC:
                return list_to_bytes(pboc_unpadding(result))
                
        return list_to_bytes(result)

    def crypt_cbc(self, iv, input_data):
        """
        CBC 模式加密/解密。
        每个块与前一块密文（或 IV）异或后再加密，提高安全性。
        """
        iv_bytes = bytes_to_list(iv)
        data_bytes = input_data
        offset = 0
        result = []
        temp_block = [0] * 16
        
        if self.mode == SM4_ENCRYPT:
            if self.padding_mode == NoPadding:
                pass
            if self.padding_mode == ZERO:
                data_bytes = zero_padding(bytes_to_list(data_bytes))
            if self.padding_mode == ISO9797M2:
                data_bytes = iso9797m2_padding(data_bytes)
            if self.padding_mode == PKCS7:
                data_bytes = pkcs7_padding(bytes_to_list(data_bytes))
            if self.padding_mode == PBOC:
                data_bytes = pboc_padding(data_bytes)
                
            length = len(data_bytes)
            while length > 0:
                temp_block[0:16] = xor(data_bytes[offset:offset + 16], iv_bytes[0:16])
                encrypted_block = self.one_round(self.sk, temp_block[0:16])
                result += encrypted_block
                iv_bytes = copy.deepcopy(encrypted_block)
                offset += 16
                length -= 16
            return list_to_bytes(result)
        else:
            length = len(data_bytes)
            while length > 0:
                decrypted_block = self.one_round(self.sk, data_bytes[offset:offset + 16])
                decrypted_block[0:16] = xor(decrypted_block[0:16], iv_bytes[0:16])
                result += decrypted_block
                iv_bytes = copy.deepcopy(data_bytes[offset:offset + 16])
                offset += 16
                length -= 16
                
            if self.padding_mode == NoPadding:
                pass
            if self.padding_mode == ZERO:
                return list_to_bytes(zero_unpadding(result))
            if self.padding_mode == ISO9797M2:
                return list_to_bytes(iso9797m2_unpadding(result))
            if self.padding_mode == PKCS7:
                return list_to_bytes(pkcs7_unpadding(result))
            if self.padding_mode == PBOC:
                return list_to_bytes(pboc_unpadding(result))
            return list_to_bytes(result)
