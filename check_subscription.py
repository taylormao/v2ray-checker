#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2rayN 订阅链接节点有效性检测脚本
支持多种订阅格式: Base64, JSON, VMess/VLESS/Trojan/SS URI
"""

import base64
import json
import re
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import argparse
from datetime import datetime


# 颜色输出 (Windows兼容)
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


@dataclass
class NodeInfo:
    """节点信息数据结构"""
    type: str  # vmess, vless, trojan, ss, socks
    address: str
    port: int
    uuid: Optional[str] = None
    aid: Optional[int] = None
    security: Optional[str] = None
    network: Optional[str] = None
    host: Optional[str] = None
    path: Optional[str] = None
    tls: Optional[bool] = None
    sni: Optional[str] = None
    remark: Optional[str] = None
    password: Optional[str] = None
    method: Optional[str] = None
    raw: Optional[str] = None
    # 检测结果
    is_valid: bool = False
    latency: float = 0.0
    error_msg: str = ""


class SubscriptionChecker:
    """订阅检测器主类"""

    def __init__(self, timeout=3, workers=30):
        self.timeout = timeout
        self.workers = workers
        self.results = []
        self.total_nodes = 0

    def fetch_subscription(self, url: str) -> str:
        """获取订阅内容"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        # 订阅抓取超时独立设置：国内网络访问 GitHub Raw 等慢源时容易超时，
        # 因此抓取超时至少给 20 秒，与节点检测超时（self.timeout）解耦
        fetch_timeout = max(self.timeout * 2, 20)
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=fetch_timeout) as response:
                content = response.read().decode('utf-8', errors='ignore')
                return content
        except socket.timeout as e:
            raise Exception(f"获取订阅超时({fetch_timeout}s): {url}") from e
        except (URLError, HTTPError) as e:
            raise Exception(f"获取订阅失败: {str(e)}") from e

    def parse_subscription(self, content: str) -> List[NodeInfo]:
        """解析订阅内容，支持多种格式"""
        nodes = []
        content = content.strip()

        # 1. 尝试Base64解码
        try:
            # 检查是否是Base64格式 (只包含合法Base64字符)
            if re.match(r'^[A-Za-z0-9+/=]+$', content):
                decoded = base64.b64decode(content).decode('utf-8', errors='ignore')
                # 如果解码后包含VMess/VLESS等关键词，说明是Base64编码的订阅
                if any(key in decoded for key in ['vmess://', 'vless://', 'trojan://', 'ss://']):
                    return self.parse_subscription(decoded)
                # 否则可能是JSON格式
                if decoded.strip().startswith('{') or decoded.strip().startswith('['):
                    return self._parse_json(decoded)
        except Exception:
            pass

        # 2. 尝试直接解析JSON
        if content.strip().startswith('{') or content.strip().startswith('['):
            return self._parse_json(content)

        # 3. 按行解析URI
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            node = self._parse_uri(line)
            if node:
                nodes.append(node)

        return nodes

    def _parse_json(self, json_str: str) -> List[NodeInfo]:
        """解析JSON格式订阅 (v2rayN格式)"""
        nodes = []
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                for item in data:
                    if 'address' in item and 'port' in item:
                        node = NodeInfo(
                            type=item.get('type', 'vmess'),
                            address=item.get('address', ''),
                            port=int(item.get('port', 0)),
                            uuid=item.get('id', item.get('uuid', '')),
                            aid=int(item.get('aid', 0)) if item.get('aid') else None,
                            security=item.get('security', 'auto'),
                            network=item.get('net', item.get('network', 'tcp')),
                            host=item.get('host', ''),
                            path=item.get('path', ''),
                            tls=item.get('tls', False) if isinstance(item.get('tls'), bool) else None,
                            sni=item.get('sni', ''),
                            remark=item.get('remark', item.get('ps', '')),
                            raw=json.dumps(item)
                        )
                        nodes.append(node)
        except json.JSONDecodeError:
            pass
        return nodes

    def _parse_uri(self, uri: str) -> Optional[NodeInfo]:
        """解析代理URI"""
        node = None
        try:
            if uri.startswith('vmess://'):
                node = self._parse_vmess(uri)
            elif uri.startswith('vless://'):
                node = self._parse_vless(uri)
            elif uri.startswith('trojan://'):
                node = self._parse_trojan(uri)
            elif uri.startswith('ss://'):
                node = self._parse_ss(uri)
            elif uri.startswith('socks://'):
                node = self._parse_socks(uri)
        except Exception as e:
            pass
        return node

    def _parse_vmess(self, uri: str) -> Optional[NodeInfo]:
        """解析VMess链接"""
        try:
            # 移除 vmess://
            b64_part = uri[8:]
            # 解码Base64
            decoded = base64.b64decode(b64_part).decode('utf-8', errors='ignore')
            data = json.loads(decoded)

            return NodeInfo(
                type='vmess',
                address=data.get('add', ''),
                port=int(data.get('port', 0)),
                uuid=data.get('id', ''),
                aid=int(data.get('aid', 0)) if data.get('aid') else None,
                security=data.get('scy', data.get('security', 'auto')),
                network=data.get('net', 'tcp'),
                host=data.get('host', ''),
                path=data.get('path', ''),
                tls=data.get('tls', '') == 'tls',
                sni=data.get('sni', ''),
                remark=data.get('ps', ''),
                raw=uri
            )
        except Exception:
            return None

    def _parse_vless(self, uri: str) -> Optional[NodeInfo]:
        """解析VLESS链接"""
        try:
            parsed = urlparse(uri)
            params = parse_qs(parsed.query)

            return NodeInfo(
                type='vless',
                address=parsed.hostname or '',
                port=parsed.port or 0,
                uuid=parsed.username or '',
                security=params.get('security', [''])[0],
                network=params.get('type', ['tcp'])[0],
                host=params.get('host', [''])[0],
                path=params.get('path', [''])[0],
                tls=params.get('security', [''])[0] in ['tls', 'reality'],
                sni=params.get('sni', [''])[0],
                remark=params.get('remark', [''])[0],
                raw=uri
            )
        except Exception:
            return None

    def _parse_trojan(self, uri: str) -> Optional[NodeInfo]:
        """解析Trojan链接"""
        try:
            parsed = urlparse(uri)
            params = parse_qs(parsed.query)

            return NodeInfo(
                type='trojan',
                address=parsed.hostname or '',
                port=parsed.port or 0,
                password=parsed.username or '',
                host=params.get('host', [''])[0],
                tls=True,
                sni=params.get('sni', [''])[0],
                remark=params.get('remark', [''])[0],
                raw=uri
            )
        except Exception:
            return None

    def _parse_ss(self, uri: str) -> Optional[NodeInfo]:
        """解析Shadowsocks链接"""
        try:
            # SS格式: ss://method:password@host:port#remark
            pattern = r'ss://([^@]+)@([^:]+):(\d+)(?:#(.+))?'
            match = re.match(pattern, uri)
            if match:
                method_pass = match.group(1)
                # 尝试Base64解码
                try:
                    decoded = base64.b64decode(method_pass).decode('utf-8', errors='ignore')
                    method, password = decoded.split(':', 1)
                except:
                    method, password = 'aes-256-gcm', method_pass

                return NodeInfo(
                    type='ss',
                    address=match.group(2),
                    port=int(match.group(3)),
                    method=method,
                    password=password,
                    remark=match.group(4) if match.group(4) else '',
                    raw=uri
                )
        except Exception:
            return None

    def _parse_socks(self, uri: str) -> Optional[NodeInfo]:
        """解析Socks链接"""
        try:
            parsed = urlparse(uri)
            return NodeInfo(
                type='socks',
                address=parsed.hostname or '',
                port=parsed.port or 0,
                remark=parsed.fragment or '',
                raw=uri
            )
        except Exception:
            return None

    def check_tcp_port(self, host: str, port: int) -> Tuple[bool, float]:
        """TCP端口连通性检测"""
        start_time = time.time()
        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                latency = (time.time() - start_time) * 1000
                return True, latency
        except Exception:
            return False, 0

    def check_tls_handshake(self, host: str, port: int, sni: Optional[str] = None) -> Tuple[bool, float]:
        """TLS握手检测"""
        start_time = time.time()
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            with socket.create_connection((host, port), timeout=self.timeout) as sock:
                with context.wrap_socket(sock, server_hostname=sni or host) as ssock:
                    # 完成TLS握手
                    ssock.getpeercert()
                    latency = (time.time() - start_time) * 1000
                    return True, latency
        except Exception:
            return False, 0

    def check_node(self, node: NodeInfo) -> NodeInfo:
        """检测单个节点"""
        if not node.address or not node.port:
            node.is_valid = False
            node.error_msg = "无效地址或端口"
            return node

        # 1. TCP端口检测
        tcp_ok, tcp_latency = self.check_tcp_port(node.address, node.port)
        if not tcp_ok:
            node.is_valid = False
            node.error_msg = f"TCP连接失败: {node.address}:{node.port}"
            return node

        # 2. 如果有TLS，进行TLS握手检测
        if node.tls:
            tls_ok, tls_latency = self.check_tls_handshake(
                node.address, node.port, node.sni or node.host
            )
            if not tls_ok:
                node.is_valid = False
                node.error_msg = "TLS握手失败"
                return node
            node.latency = tls_latency
        else:
            node.latency = tcp_latency

        node.is_valid = True
        node.error_msg = "正常"
        return node

    def check_subscription(self, urls: List[str]) -> List[NodeInfo]:
        """检测订阅中的所有节点"""
        all_nodes = []

        # 获取并解析所有订阅
        for url in urls:
            print(f"{Colors.BLUE}📡 获取订阅: {url}{Colors.RESET}")
            try:
                content = self.fetch_subscription(url)
                nodes = self.parse_subscription(content)
                print(f"{Colors.GREEN}   ✅ 解析到 {len(nodes)} 个节点{Colors.RESET}")
                all_nodes.extend(nodes)
            except Exception as e:
                print(f"{Colors.RED}   ❌ {str(e)}{Colors.RESET}")

        return self._run_detection(all_nodes)

    def check_nodes_text(self, nodes_text: str) -> List[NodeInfo]:
        """直接检测节点列表（不走订阅抓取，每行一个节点URI）"""
        all_nodes = []
        for line in nodes_text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            node = self._parse_uri(line)
            if node:
                all_nodes.append(node)
            else:
                print(f"{Colors.YELLOW}   ⚠️ 无法解析: {line[:60]}{Colors.RESET}")

        return self._run_detection(all_nodes)

    def _run_detection(self, all_nodes: List[NodeInfo]) -> List[NodeInfo]:
        """公共检测流程：解析结果 → 并发检测 → 返回有效节点"""
        self.total_nodes = len(all_nodes)
        if not all_nodes:
            print(f"{Colors.RED}没有解析到任何节点{Colors.RESET}")
            return []

        print(
            f"\n{Colors.CYAN}🔍 开始检测 {len(all_nodes)} 个节点 (超时: {self.timeout}s, 并发: {self.workers}){Colors.RESET}")
        print("-" * 60)

        # 并发检测节点
        valid_nodes = []
        checked = 0

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_node = {
                executor.submit(self.check_node, node): node
                for node in all_nodes
            }

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                try:
                    result = future.result()
                    checked += 1

                    if result.is_valid:
                        valid_nodes.append(result)
                        status = f"{Colors.GREEN}✅ 有效{Colors.RESET}"
                        lat = f"{result.latency:.1f}ms"
                        print(
                            f"[{checked}/{len(all_nodes)}] {status} {lat:>8}  {result.type.upper():6} {result.remark or result.address}:{result.port}")
                    else:
                        status = f"{Colors.RED}❌ 无效{Colors.RESET}"
                        print(
                            f"[{checked}/{len(all_nodes)}] {status}        {result.type.upper():6} {result.remark or result.address}:{result.port}  {result.error_msg[:40]}")
                except Exception as e:
                    checked += 1
                    print(f"[{checked}/{len(all_nodes)}] {Colors.RED}❌ 异常   {str(e)[:50]}{Colors.RESET}")

        return valid_nodes

    def generate_shareable_subscription(self, nodes: List[NodeInfo]) -> str:
        """生成可用的订阅内容"""
        uris = []
        for node in nodes:
            if node.raw:
                uris.append(node.raw)
        return '\n'.join(uris)

    def print_summary(self, total: int, valid: List[NodeInfo]):
        """打印检测结果摘要"""
        print("\n" + "=" * 60)
        print(f"{Colors.BOLD}📊 检测完成!{Colors.RESET}")
        print(f"总计节点: {total}")
        print(f"{Colors.GREEN}✅ 有效节点: {len(valid)}{Colors.RESET}")
        print(f"{Colors.RED}❌ 无效节点: {total - len(valid)}{Colors.RESET}")

        if valid:
            # 按延迟排序
            valid_sorted = sorted(valid, key=lambda x: x.latency)
            print(f"\n{Colors.BOLD}🏆 最快节点 TOP 5:{Colors.RESET}")
            for i, node in enumerate(valid_sorted[:5], 1):
                latency_color = Colors.GREEN if node.latency < 100 else Colors.YELLOW if node.latency < 300 else Colors.RED
                print(
                    f"  {i}. {node.type.upper()} {node.remark or node.address}:{node.port}  {latency_color}{node.latency:.1f}ms{Colors.RESET}")


def main():
    parser = argparse.ArgumentParser(description='v2rayN订阅节点有效性检测工具')
    parser.add_argument('-u', '--url', action='append', help='订阅链接 (可多次使用)')
    parser.add_argument('-f', '--file', help='包含订阅链接的文件 (每行一个)')
    parser.add_argument('-n', '--nodes', help='节点列表文件 (每行一个节点URI, 如 vless://, 不走订阅抓取)')
    parser.add_argument('-t', '--timeout', type=int, default=3, help='检测超时时间(秒), 默认3')
    parser.add_argument('-w', '--workers', type=int, default=30, help='并发线程数, 默认30')
    parser.add_argument('-o', '--output', help='输出可用订阅到文件')
    parser.add_argument('--no-color', action='store_true', help='禁用颜色输出')
    args = parser.parse_args()

    # 禁用颜色
    if args.no_color:
        for attr in dir(Colors):
            if not attr.startswith('_'):
                setattr(Colors, attr, '')

    # 收集订阅链接
    urls = args.url or []
    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        urls.append(line)
        except FileNotFoundError:
            print(f"{Colors.RED}文件不存在: {args.file}{Colors.RESET}")
            sys.exit(1)

    # 节点列表文件（模式二：节点批量检测）
    nodes_text = None
    if args.nodes:
        try:
            with open(args.nodes, 'r', encoding='utf-8') as f:
                nodes_text = f.read()
        except FileNotFoundError:
            print(f"{Colors.RED}文件不存在: {args.nodes}{Colors.RESET}")
            sys.exit(1)

    if not urls and not nodes_text:
        print("请提供输入: -u <订阅链接> / -f <链接文件> 或 -n <节点列表文件>")
        parser.print_help()
        sys.exit(1)

    print(f"{Colors.CYAN}🚀 v2rayN 订阅节点检测工具{Colors.RESET}")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 60)

    # 执行检测
    checker = SubscriptionChecker(timeout=args.timeout, workers=args.workers)
    if nodes_text:
        valid_nodes = checker.check_nodes_text(nodes_text)
    else:
        valid_nodes = checker.check_subscription(urls)

    # 输出结果
    checker.print_summary(checker.total_nodes, valid_nodes)

    # 保存结果
    if args.output and valid_nodes:
        content = checker.generate_shareable_subscription(valid_nodes)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"\n{Colors.GREEN}💾 可用订阅已保存到: {args.output}{Colors.RESET}")


if __name__ == '__main__':
    main()