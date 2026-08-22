#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2rayN 订阅节点检测 Web UI
支持多订阅链接输入，实时显示检测进度
"""

# 必须最先 import eventlet 并完成 monkey_patch，
# 否则 patch 已加载的 flask/werkzeug 实例会触发
# "Working outside of application context" 警告
import eventlet
eventlet.monkey_patch()

import os
import sys
import json
import time
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'v2ray-checker-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# 全局变量
checking_process = None
checking_status = {
    'is_running': False,
    'progress': 0,
    'total_nodes': 0,
    'checked_nodes': 0,
    'valid_nodes': 0,
    'invalid_nodes': 0,
    'current_node': '',
    'log_messages': [],
    'start_time': None,
    'end_time': None
}

# 结果目录
RESULT_DIR = Path(__file__).parent / 'result'
RESULT_DIR.mkdir(exist_ok=True)

# 节点临时输入目录
TEMP_DIR = Path(__file__).parent / 'temp'
TEMP_DIR.mkdir(exist_ok=True)


def reset_status():
    """重置检测状态"""
    global checking_status
    checking_status = {
        'is_running': True,
        'progress': 0,
        'total_nodes': 0,
        'checked_nodes': 0,
        'valid_nodes': 0,
        'invalid_nodes': 0,
        'current_node': '',
        'log_messages': [],
        'start_time': datetime.now().isoformat(),
        'end_time': None
    }


@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/check', methods=['POST'])
def start_check():
    """启动订阅链接检测"""
    global checking_process

    if checking_status['is_running']:
        return jsonify({'error': '检测正在进行中'}), 400

    data = request.json
    urls = data.get('urls', [])
    timeout = data.get('timeout', 3)
    workers = data.get('workers', 30)

    if not urls:
        return jsonify({'error': '请至少输入一个订阅链接'}), 400

    # 重置状态
    reset_status()

    # 在后台线程中运行检测
    thread = threading.Thread(
        target=run_check,
        args=(urls, timeout, workers, 'url')
    )
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started'})


@app.route('/api/check_nodes', methods=['POST'])
def start_nodes_check():
    """启动节点批量检测（直接解析节点URI，不走订阅抓取）"""
    global checking_process

    if checking_status['is_running']:
        return jsonify({'error': '检测正在进行中'}), 400

    data = request.json
    nodes = data.get('nodes', [])
    timeout = data.get('timeout', 3)
    workers = data.get('workers', 30)

    if not nodes:
        return jsonify({'error': '请至少输入一个节点'}), 400

    # 将节点列表写入临时文件，供子进程 -n 读取
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    nodes_file = TEMP_DIR / f'nodes_input_{timestamp}.txt'
    with open(nodes_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(nodes))

    # 重置状态
    reset_status()

    # 在后台线程中运行检测
    thread = threading.Thread(
        target=run_check,
        args=([str(nodes_file)], timeout, workers, 'nodes')
    )
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'started'})


@app.route('/api/stop', methods=['POST'])
def stop_check():
    """停止检测"""
    global checking_process, checking_status

    if checking_process and checking_process.poll() is None:
        checking_process.terminate()
        checking_process = None
        checking_status['is_running'] = False
        checking_status['end_time'] = datetime.now().isoformat()
        add_log('⚠️ 检测已被用户停止')
        return jsonify({'status': 'stopped'})

    return jsonify({'status': 'not_running'})


@app.route('/api/status')
def get_status():
    """获取检测状态"""
    return jsonify(checking_status)


@app.route('/api/results')
def get_results():
    """获取结果列表"""
    results = []
    for file in RESULT_DIR.glob('*.txt'):
        stat = file.stat()
        results.append({
            'name': file.name,
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'path': str(file)
        })
    # 按修改时间排序，最新的在前
    results.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify(results)


@app.route('/api/result/<filename>')
def download_result(filename):
    """下载结果文件"""
    file_path = RESULT_DIR / filename
    if file_path.exists():
        return send_file(file_path, as_attachment=True)
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/clear_results', methods=['POST'])
def clear_results():
    """清空结果文件"""
    try:
        for file in RESULT_DIR.glob('*.txt'):
            file.unlink()
        return jsonify({'status': 'cleared'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('connect')
def handle_connect():
    """WebSocket连接"""
    emit('status', checking_status)


def add_log(message, level='info'):
    """添加日志消息"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    log_entry = {
        'timestamp': timestamp,
        'message': message,
        'level': level
    }
    checking_status['log_messages'].append(log_entry)
    # 只保留最近200条日志
    if len(checking_status['log_messages']) > 200:
        checking_status['log_messages'] = checking_status['log_messages'][-200:]

    # 通过WebSocket推送
    socketio.emit('log', log_entry)
    socketio.emit('status', checking_status)


def run_check(items, timeout, workers, mode='url'):
    """执行检测任务
    mode='url'  : items 为订阅链接列表，走 -u 抓取
    mode='nodes': items 为 [节点文件路径]，走 -n 直接解析
    """
    global checking_process, checking_status

    try:
        # 生成结果文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        result_file = RESULT_DIR / f'valid_nodes_{timestamp}.txt'
        log_file = RESULT_DIR / f'check_log_{timestamp}.txt'

        if mode == 'nodes':
            add_log(f'🚀 开始节点批量检测')
        else:
            add_log(f'🚀 开始检测 {len(items)} 个订阅链接')
        add_log(f'⏱️  超时设置: {timeout}s, 并发数: {workers}')

        # 构建命令
        cmd = [
            sys.executable,
            'check_subscription.py'
        ]

        if mode == 'nodes':
            # 节点批量检测：直接传节点文件
            cmd.extend(['-n', items[0]])
        else:
            # 订阅链接检测：添加所有URL
            for url in items:
                if url.strip():
                    cmd.extend(['-u', url.strip()])

        cmd.extend(['-t', str(timeout)])
        cmd.extend(['-w', str(workers)])
        cmd.extend(['-o', str(result_file)])
        cmd.append('--no-color')  # 禁用颜色输出便于日志

        add_log(f'📝 执行命令: {" ".join(cmd)}')

        # 启动子进程（强制 UTF-8 编码，避免 Windows 下 GBK 无法编码 emoji 导致子进程崩溃）
        child_env = os.environ.copy()
        child_env['PYTHONIOENCODING'] = 'utf-8'
        checking_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            universal_newlines=True,
            env=child_env
        )

        # 读取输出
        line_count = 0
        for line in iter(checking_process.stdout.readline, ''):
            if not line:
                break

            line = line.strip()
            if line:
                # 保存到日志文件
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')

                # 解析进度信息
                parse_progress(line)
                add_log(line, 'info')
                line_count += 1

        # 等待进程结束
        return_code = checking_process.wait()
        checking_process = None

        if return_code == 0:
            add_log('✅ 检测完成！')
            checking_status['is_running'] = False
            checking_status['end_time'] = datetime.now().isoformat()

            # 读取统计信息
            if result_file.exists():
                with open(result_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    node_count = content.count('://')
                    add_log(f'📊 共检测到 {node_count} 个有效节点')
                    add_log(f'💾 结果已保存到: {result_file.name}')
        else:
            add_log(f'❌ 检测异常退出，返回码: {return_code}')
            checking_status['is_running'] = False
            checking_status['end_time'] = datetime.now().isoformat()

    except Exception as e:
        add_log(f'❌ 检测过程出错: {str(e)}')
        checking_status['is_running'] = False
        checking_status['end_time'] = datetime.now().isoformat()


def parse_progress(line):
    """解析进度信息"""
    # 匹配格式: [1/46] ✅ 有效   45.2ms  VMESS  us-node:443
    import re

    # 更新总节点数
    total_match = re.search(r'检测 (\d+) 个节点', line)
    if total_match:
        checking_status['total_nodes'] = int(total_match.group(1))

    # 更新进度
    progress_match = re.search(r'\[(\d+)/(\d+)\]', line)
    if progress_match:
        checked = int(progress_match.group(1))
        total = int(progress_match.group(2))
        checking_status['checked_nodes'] = checked
        checking_status['total_nodes'] = total
        if total > 0:
            checking_status['progress'] = int((checked / total) * 100)

    # 更新有效/无效节点数（仅统计 [n/m] 进度行，避免摘要行误计）
    if re.search(r'\[\d+/\d+\]\s+✅ 有效', line):
        checking_status['valid_nodes'] += 1
    elif re.search(r'\[\d+/\d+\]\s+❌ (无效|异常)', line):
        checking_status['invalid_nodes'] += 1

    # 提取当前节点信息
    node_match = re.search(r'✅ 有效\s+[\d.]+\s*ms\s+\w+\s+([^:]+):\d+', line)
    if node_match:
        checking_status['current_node'] = node_match.group(1)

    # 推送状态更新
    socketio.emit('status', checking_status)


if __name__ == '__main__':
    print("🚀 v2rayN 订阅节点检测 Web UI")
    print(f"📁 结果目录: {RESULT_DIR.absolute()}")
    print("🌐 访问地址: http://localhost:5000")
    print("按 Ctrl+C 停止服务")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)