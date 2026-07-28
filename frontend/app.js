// TikTok Controller Frontend Application

class TikTokController {
    constructor() {
        this.apiBase = '/api';
        this.devices = [];
        this.selectedDevice = null;
        this.selectedDevices = new Set();
        this.isStreaming = false;
        this.ws = null;
        this.config = {};
        this.calibrationMode = null;

        this.init();
    }

    async init() {
        await this.loadConfig();
        await this.loadDevices();
        this.bindEvents();
        this.updateConnectionStatus(true);
    }

    // API Methods
    async api(endpoint, method = 'GET', body = null) {
        const options = {
            method,
            headers: { 'Content-Type': 'application/json' }
        };
        if (body) {
            options.body = JSON.stringify(body);
        }

        try {
            const response = await fetch(`${this.apiBase}${endpoint}`, options);
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'API error');
            }
            return await response.json();
        } catch (error) {
            this.showToast(error.message, 'error');
            throw error;
        }
    }

    // Configuration
    async loadConfig() {
        try {
            this.config = await this.api('/config');
            this.updateSettingsUI();
        } catch (error) {
            console.error('Failed to load config:', error);
        }
    }

    async saveConfig() {
        const settings = {
            imouse_host: document.getElementById('setting-host').value || undefined,
            imouse_port: parseInt(document.getElementById('setting-port').value) || undefined,
            anthropic_api_key: document.getElementById('setting-anthropic-key').value || undefined,
            anthropic_model: document.getElementById('setting-anthropic-model').value || undefined
        };

        // Remove undefined values
        Object.keys(settings).forEach(key => {
            if (settings[key] === undefined) delete settings[key];
        });

        try {
            await this.api('/config', 'POST', settings);
            this.showToast('Settings saved', 'success');
            this.closeModal();
            await this.loadConfig();
        } catch (error) {
            console.error('Failed to save config:', error);
        }
    }

    updateSettingsUI() {
        document.getElementById('setting-host').value = this.config.imouse_host || '';
        document.getElementById('setting-port').value = this.config.imouse_port || '';
        document.getElementById('setting-anthropic-model').value =
            this.config.anthropic_model || 'claude-haiku-4-5-20251001';
    }

    // Device Management
    async loadDevices() {
        try {
            const data = await this.api('/devices');
            this.devices = data.devices;
            this.renderDevices();
        } catch (error) {
            console.error('Failed to load devices:', error);
        }
    }

    renderDevices() {
        const container = document.getElementById('device-list');

        if (this.devices.length === 0) {
            container.innerHTML = '<p class="placeholder">No devices found</p>';
            return;
        }

        container.innerHTML = this.devices.map(device => `
            <div class="device-item ${device.device_id === this.selectedDevice?.device_id ? 'selected' : ''}
                        ${this.selectedDevices.has(device.device_id) ? 'multi-selected' : ''}"
                 data-device-id="${device.device_id}">
                <div class="device-status ${device.is_online ? 'online' : ''}"></div>
                <div class="device-info">
                    <div class="device-name">${device.name || device.device_id.slice(0, 8)}</div>
                    <div class="device-model">${device.model} (${device.width}x${device.height})</div>
                </div>
            </div>
        `).join('');
    }

    selectDevice(deviceId, multiSelect = false) {
        const device = this.devices.find(d => d.device_id === deviceId);
        if (!device) return;

        if (multiSelect) {
            if (this.selectedDevices.has(deviceId)) {
                this.selectedDevices.delete(deviceId);
            } else {
                this.selectedDevices.add(deviceId);
            }
            document.getElementById('batch-actions').style.display =
                this.selectedDevices.size > 1 ? 'block' : 'none';
        } else {
            this.selectedDevice = device;
            this.selectedDevices.clear();
            this.selectedDevices.add(deviceId);
            // Start a live view immediately so the preview is instant, not a
            // single delayed still.
            this.stopStream();
            this.startStream();
        }

        this.renderDevices();
        this.updateActionButtons();
    }

    // Screenshot & Streaming

    // Reuse a single <img> so frames swap in place instead of rebuilding the
    // DOM node every frame (that caused flicker and slowdown).
    ensurePreviewImg() {
        const preview = document.getElementById('device-preview');
        let img = preview.querySelector('img');
        if (!img) {
            preview.innerHTML = '';
            img = document.createElement('img');
            img.alt = 'Device screen';
            preview.appendChild(img);
        }
        return img;
    }

    setPreviewSrc(src) {
        this.ensurePreviewImg().src = src;
    }

    // Display a raw JPEG blob via an object URL, revoking the previous one once
    // the new frame has decoded (avoids a memory leak during streaming).
    setPreviewBlob(blob) {
        const url = URL.createObjectURL(blob);
        const img = this.ensurePreviewImg();
        const previous = this._previewUrl;
        img.onload = () => { if (previous) URL.revokeObjectURL(previous); };
        img.src = url;
        this._previewUrl = url;
    }

    async updateScreenshot() {
        if (!this.selectedDevice) return;

        try {
            const data = await this.api(`/devices/${this.selectedDevice.device_id}/screenshot`);
            this.setPreviewSrc(`data:image/jpeg;base64,${data.screenshot}`);
        } catch (error) {
            document.getElementById('device-preview').innerHTML =
                '<p class="placeholder">Failed to load screenshot</p>';
        }
    }

    startStream() {
        if (!this.selectedDevice || this.isStreaming) return;

        this.ws = new WebSocket(`ws://${window.location.host}/ws`);
        this.ws.binaryType = 'blob';   // receive JPEG frames as binary, not JSON

        this.ws.onopen = () => {
            this.ws.send(JSON.stringify({
                action: 'start_stream',
                device_id: this.selectedDevice.device_id,
                interval: 80   // ~12 fps ceiling; actual rate paced by fetch time
            }));
            this.isStreaming = true;
            document.getElementById('toggle-stream').textContent = 'Stop Stream';
        };

        this.ws.onmessage = (event) => {
            if (event.data instanceof Blob) {
                this.setPreviewBlob(event.data);
            } else {
                // JSON fallback (kept for compatibility with older servers).
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'screenshot' && data.data) {
                        this.setPreviewSrc(`data:image/jpeg;base64,${data.data}`);
                    }
                } catch (e) { /* ignore non-frame messages */ }
            }
        };

        this.ws.onclose = () => {
            this.isStreaming = false;
            document.getElementById('toggle-stream').textContent = 'Start Stream';
        };
    }

    stopStream() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.isStreaming = false;
        document.getElementById('toggle-stream').textContent = 'Start Stream';
    }

    toggleStream() {
        if (this.isStreaming) {
            this.stopStream();
        } else {
            this.startStream();
        }
    }

    // Actions
    async performAction(action, deviceId = null) {
        const id = deviceId || this.selectedDevice?.device_id;
        if (!id) return;

        const endpoints = {
            'swipe-next': `/devices/${id}/swipe/next`,
            'swipe-previous': `/devices/${id}/swipe/previous`,
            'like': `/devices/${id}/like`,
            'save': `/devices/${id}/save`,
            'follow': `/devices/${id}/follow`
        };

        const endpoint = endpoints[action];
        if (!endpoint) return;

        try {
            const result = await this.api(endpoint, 'POST');
            if (result.success) {
                this.showToast(`${action} successful`, 'success');
                // The live stream already refreshes the preview; only pull a
                // one-off shot when streaming is off.
                if (!this.isStreaming) {
                    setTimeout(() => this.updateScreenshot(), 500);
                }
            } else {
                this.showToast(result.message || 'Action failed', 'error');
            }
        } catch (error) {
            console.error(`Action ${action} failed:`, error);
        }
    }

    async batchAction(action) {
        if (this.selectedDevices.size < 2) return;

        const deviceIds = Array.from(this.selectedDevices);

        try {
            const result = await this.api('/batch/action', 'POST', {
                device_ids: deviceIds,
                action: action
            });

            const successCount = Object.values(result.results).filter(r => r.success).length;
            this.showToast(`${action}: ${successCount}/${deviceIds.length} successful`,
                successCount === deviceIds.length ? 'success' : 'warning');
        } catch (error) {
            console.error(`Batch ${action} failed:`, error);
        }
    }

    // Comments
    async generateComments() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }

        const container = document.getElementById('comment-suggestions');
        container.innerHTML = '<p class="placeholder loading"></p>';

        try {
            const style = document.getElementById('comment-style').value;
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/generate-comments`, 'POST', {
                device_id: this.selectedDevice.device_id,
                count: 3,
                styles: [style, 'friendly', 'funny']
            });

            container.innerHTML = result.comments.map((comment, i) => `
                <div class="comment-suggestion" data-comment="${this.escapeHtml(comment)}">
                    ${this.escapeHtml(comment)}
                </div>
            `).join('');

        } catch (error) {
            container.innerHTML = '<p class="placeholder">Failed to generate comments</p>';
        }
    }

    async postComment() {
        if (!this.selectedDevice) return;

        const customComment = document.getElementById('custom-comment').value;
        const selectedSuggestion = document.querySelector('.comment-suggestion.selected');
        const comment = customComment || selectedSuggestion?.dataset.comment;

        if (!comment) {
            this.showToast('Please enter or select a comment', 'warning');
            return;
        }

        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/comment`, 'POST', {
                device_id: this.selectedDevice.device_id,
                comment: comment,
                use_ai: false
            });

            if (result.success) {
                this.showToast('Comment posted', 'success');
                document.getElementById('custom-comment').value = '';
                document.querySelectorAll('.comment-suggestion').forEach(el => el.classList.remove('selected'));
            } else {
                this.showToast(result.message || 'Failed to post comment', 'error');
            }
        } catch (error) {
            console.error('Failed to post comment:', error);
        }
    }

    // Video Analysis
    async analyzeVideo() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }

        const container = document.getElementById('analysis-result');
        container.innerHTML = '<p class="placeholder loading"></p>';

        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/analyze`);

            let html = '';
            if (result.caption) {
                html += `<p><strong>Caption:</strong> ${this.escapeHtml(result.caption)}</p>`;
            }
            if (result.username) {
                html += `<p><strong>Creator:</strong> ${this.escapeHtml(result.username)}</p>`;
            }
            if (result.hashtags && result.hashtags.length > 0) {
                html += `<p><strong>Hashtags:</strong> ${result.hashtags.map(h =>
                    `<span class="hashtag">${this.escapeHtml(h)}</span>`
                ).join(' ')}</p>`;
            }
            if (result.ocr_text) {
                html += `<p><strong>Content:</strong> ${this.escapeHtml(result.ocr_text)}</p>`;
            }

            container.innerHTML = html || '<p class="placeholder">No content detected</p>';
        } catch (error) {
            container.innerHTML = '<p class="placeholder">Analysis failed</p>';
        }
    }

    // AI Agent
    renderAgentStep(step) {
        const log = document.getElementById('agent-log');
        if (log.querySelector('.placeholder')) log.innerHTML = '';
        const ok = step.success ? 'ok' : 'fail';
        const args = step.args && Object.keys(step.args).length
            ? ' ' + this.escapeHtml(JSON.stringify(step.args)) : '';
        const row = document.createElement('div');
        row.className = `agent-step ${ok}`;
        row.innerHTML = `<strong>${this.escapeHtml(step.action || 'error')}</strong>${args}` +
            ` <span class="agent-msg">${this.escapeHtml(step.message || '')}</span>`;
        log.appendChild(row);
        log.scrollTop = log.scrollHeight;
    }

    async agentStep() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const goal = document.getElementById('agent-goal').value || undefined;
        try {
            const step = await this.api(`/devices/${this.selectedDevice.device_id}/ai/step`,
                'POST', { goal });
            this.renderAgentStep(step);
        } catch (error) {
            console.error('Agent step failed:', error);
        }
    }

    async agentRun() {
        if (!this.selectedDevice) return;
        if (!this.config.has_anthropic_key) {
            this.showToast('Anthropic API key not configured', 'error');
            this.openModal();
            return;
        }
        const goal = document.getElementById('agent-goal').value || undefined;
        const maxSteps = parseInt(document.getElementById('agent-steps').value) || 10;
        const runBtn = document.getElementById('agent-run');
        runBtn.disabled = true;
        runBtn.textContent = 'Running…';
        try {
            const result = await this.api(`/devices/${this.selectedDevice.device_id}/ai/run`,
                'POST', { goal, max_steps: maxSteps });
            document.getElementById('agent-log').innerHTML = '';
            result.steps.forEach(s => this.renderAgentStep(s));
            this.showToast(`Agent ran ${result.count} steps`, 'success');
        } catch (error) {
            console.error('Agent run failed:', error);
        } finally {
            runBtn.disabled = false;
            runBtn.textContent = 'Run Agent';
        }
    }

    // Manual Keyboard
    async manualType() {
        if (!this.selectedDevice) return;
        const input = document.getElementById('manual-text');
        const text = input.value;
        if (!text) return;
        try {
            const res = await this.api(`/devices/${this.selectedDevice.device_id}/type`,
                'POST', { text });
            if (res.success) {
                this.showToast('Text sent', 'success');
                input.value = '';
            } else {
                this.showToast(res.message || 'Failed to send text', 'error');
            }
        } catch (error) {
            console.error('Manual type failed:', error);
        }
    }

    async manualKey(fnKey) {
        if (!this.selectedDevice) return;
        try {
            await this.api(`/devices/${this.selectedDevice.device_id}/key`,
                'POST', { fn_key: fnKey });
        } catch (error) {
            console.error('Manual key failed:', error);
        }
    }

    // UI Calibration
    startCalibration(element) {
        this.calibrationMode = element;
        this.showToast(`Click on the ${element.replace('_', ' ')} in the preview`, 'info');

        const preview = document.getElementById('device-preview');
        preview.style.cursor = 'crosshair';
    }

    async handleCalibrationClick(event) {
        if (!this.calibrationMode || !this.selectedDevice) return;

        const preview = document.getElementById('device-preview');
        const rect = preview.getBoundingClientRect();
        const img = preview.querySelector('img');

        if (!img) return;

        // Calculate relative position
        const x = (event.clientX - rect.left) / rect.width;
        const y = (event.clientY - rect.top) / rect.height;

        try {
            await this.api('/config/ui', 'POST', {
                element: this.calibrationMode,
                x: x,
                y: y
            });

            this.showToast(`${this.calibrationMode} calibrated`, 'success');
        } catch (error) {
            console.error('Calibration failed:', error);
        }

        this.calibrationMode = null;
        preview.style.cursor = 'default';
    }

    // UI Helpers
    updateConnectionStatus(connected) {
        const status = document.getElementById('connection-status');
        status.textContent = connected ? 'Connected' : 'Disconnected';
        status.className = `status ${connected ? 'connected' : 'disconnected'}`;
    }

    updateActionButtons() {
        const hasDevice = !!this.selectedDevice;
        document.querySelectorAll('.action-btn, #generate-comments, #post-comment, #analyze-video, #toggle-stream, #take-screenshot, #agent-step, #agent-run, #manual-send, .manual-key').forEach(btn => {
            btn.disabled = !hasDevice;
        });
    }

    openModal() {
        document.getElementById('settings-modal').classList.add('open');
    }

    closeModal() {
        document.getElementById('settings-modal').classList.remove('open');
    }

    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        setTimeout(() => toast.remove(), 3000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Event Binding
    bindEvents() {
        // Device selection
        document.getElementById('device-list').addEventListener('click', (e) => {
            const item = e.target.closest('.device-item');
            if (item) {
                this.selectDevice(item.dataset.deviceId, e.ctrlKey || e.metaKey);
            }
        });

        // Refresh devices
        document.getElementById('refresh-devices').addEventListener('click', () => this.loadDevices());

        // Action buttons
        document.getElementById('action-swipe-up').addEventListener('click', () => this.performAction('swipe-next'));
        document.getElementById('action-swipe-down').addEventListener('click', () => this.performAction('swipe-previous'));
        document.getElementById('action-like').addEventListener('click', () => this.performAction('like'));
        document.getElementById('action-save').addEventListener('click', () => this.performAction('save'));
        document.getElementById('action-follow').addEventListener('click', () => this.performAction('follow'));
        document.getElementById('action-comment').addEventListener('click', () => {
            document.querySelector('.comment-panel').scrollIntoView({ behavior: 'smooth' });
        });

        // Batch actions
        document.getElementById('batch-swipe').addEventListener('click', () => this.batchAction('swipe_next'));
        document.getElementById('batch-like').addEventListener('click', () => this.batchAction('like'));

        // Screenshot controls
        document.getElementById('toggle-stream').addEventListener('click', () => this.toggleStream());
        document.getElementById('take-screenshot').addEventListener('click', () => this.updateScreenshot());

        // Comments
        document.getElementById('generate-comments').addEventListener('click', () => this.generateComments());
        document.getElementById('post-comment').addEventListener('click', () => this.postComment());

        document.getElementById('comment-suggestions').addEventListener('click', (e) => {
            const suggestion = e.target.closest('.comment-suggestion');
            if (suggestion) {
                document.querySelectorAll('.comment-suggestion').forEach(el => el.classList.remove('selected'));
                suggestion.classList.add('selected');
                document.getElementById('custom-comment').value = suggestion.dataset.comment;
            }
        });

        // Analysis
        document.getElementById('analyze-video').addEventListener('click', () => this.analyzeVideo());

        // AI Agent
        document.getElementById('agent-step').addEventListener('click', () => this.agentStep());
        document.getElementById('agent-run').addEventListener('click', () => this.agentRun());

        // Manual Keyboard
        document.getElementById('manual-send').addEventListener('click', () => this.manualType());
        document.getElementById('manual-text').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); this.manualType(); }
        });
        document.querySelectorAll('.manual-key').forEach(btn => {
            btn.addEventListener('click', () => this.manualKey(btn.dataset.fn));
        });

        // Settings
        document.getElementById('settings-btn').addEventListener('click', () => this.openModal());
        document.querySelector('.close-btn').addEventListener('click', () => this.closeModal());
        document.getElementById('save-settings').addEventListener('click', () => this.saveConfig());

        // Calibration
        document.getElementById('calibration-element').addEventListener('change', (e) => {
            if (e.target.value) {
                this.startCalibration(e.target.value);
            }
        });

        document.getElementById('device-preview').addEventListener('click', (e) => {
            if (this.calibrationMode) {
                this.handleCalibrationClick(e);
            }
        });

        // Modal close on outside click
        document.getElementById('settings-modal').addEventListener('click', (e) => {
            if (e.target.id === 'settings-modal') {
                this.closeModal();
            }
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch(e.key) {
                case 'ArrowUp':
                    e.preventDefault();
                    this.performAction('swipe-previous');
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.performAction('swipe-next');
                    break;
                case 'l':
                    this.performAction('like');
                    break;
                case 's':
                    this.performAction('save');
                    break;
                case 'f':
                    this.performAction('follow');
                    break;
            }
        });
    }
}

// Initialize app
document.addEventListener('DOMContentLoaded', () => {
    window.app = new TikTokController();
});
