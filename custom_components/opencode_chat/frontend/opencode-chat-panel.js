class OpenCodeChatPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._sessions = [];
    this._activeSessionId = null;
    this._sessionData = {};
    this._streaming = false;
    this._msgId = 0;
    this._pendingSubscriptions = {};
    this._markedReady = false;
    this.attachShadow({ mode: 'open' });
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._render();
      this._loadSessions();
    }
  }

  async _loadMarked() {
    if (window.marked) { this._markedReady = true; return; }
    try {
      const script = document.createElement('script');
      script.src = '/opencode_chat_static/marked.min.js';
      script.onload = () => { this._markedReady = true; };
      script.onerror = () => { this._markedReady = false; };
      document.head.appendChild(script);
      await new Promise((resolve) => {
        const check = () => { if (this._markedReady !== undefined) resolve(); else setTimeout(check, 50); };
        check();
      });
    } catch {
      this._markedReady = false;
    }
  }

  _render() {
    const shadow = this.shadowRoot;
    shadow.innerHTML = `
      <style>
        :host { display: flex; height: 100%; width: 100%; overflow: hidden; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .sidebar { width: 260px; min-width: 260px; background: var(--sidebar-background-color, var(--primary-background-color, #f5f5f5)); border-right: 1px solid var(--divider-color, rgba(0,0,0,0.12)); display: flex; flex-direction: column; }
        .sidebar-header { padding: 12px 16px; font-weight: 500; font-size: 14px; color: var(--primary-text-color); border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.12)); display: flex; align-items: center; justify-content: space-between; }
        .sidebar-header button { background: var(--primary-color); color: white; border: none; border-radius: 4px; padding: 4px 12px; cursor: pointer; font-size: 12px; }
        .session-list { flex: 1; overflow-y: auto; padding: 4px 0; }
        .session-item { padding: 8px 16px; cursor: pointer; border-left: 3px solid transparent; font-size: 13px; color: var(--secondary-text-color); display: flex; align-items: center; justify-content: space-between; }
        .session-item:hover { background: var(--sidebar-selected-background-color, rgba(0,0,0,0.04)); }
        .session-item.active { border-left-color: var(--primary-color); background: var(--sidebar-selected-background-color, rgba(0,0,0,0.08)); color: var(--primary-text-color); font-weight: 500; }
        .session-item .title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .session-item .delete-btn { opacity: 0; background: none; border: none; cursor: pointer; color: var(--error-color, #db4437); font-size: 14px; padding: 2px 4px; }
        .session-item:hover .delete-btn { opacity: 0.6; }
        .session-item .delete-btn:hover { opacity: 1; }
        .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
        .chat-header { padding: 12px 20px; border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.12)); font-weight: 500; color: var(--primary-text-color); display: flex; align-items: center; gap: 12px; }
        .chat-header .title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .messages { flex: 1; overflow-y: auto; padding: 16px 20px; }
        .empty-state { display: flex; align-items: center; justify-content: center; height: 100%; color: var(--secondary-text-color); flex-direction: column; gap: 8px; }
        .empty-state .icon { font-size: 48px; opacity: 0.3; }
        .message { margin-bottom: 16px; max-width: 85%; }
        .message.user { margin-left: auto; }
        .message.assistant { margin-right: auto; }
        .message.tool { margin-right: auto; opacity: 0.8; font-size: 13px; }
        .bubble { padding: 10px 14px; border-radius: 12px; line-height: 1.5; font-size: 14px; word-wrap: break-word; }
        .message.user .bubble { background: var(--primary-color); color: white; border-bottom-right-radius: 4px; }
        .message.assistant .bubble { background: var(--secondary-background-color, #f1f1f1); border-bottom-left-radius: 4px; color: var(--primary-text-color); }
        .message.tool .bubble { background: transparent; padding: 6px 0; color: var(--secondary-text-color); }
        .bubble p { margin: 4px 0; }
        .bubble pre { background: var(--code-background-color, #2d2d2d); color: #ccc; padding: 8px 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; max-width: 100%; }
        .bubble code { font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; }
        .bubble p > code { background: rgba(0,0,0,0.08); padding: 1px 4px; border-radius: 3px; }
        .bubble table { border-collapse: collapse; margin: 8px 0; font-size: 13px; width: 100%; }
        .bubble th, .bubble td { border: 1px solid var(--divider-color, rgba(0,0,0,0.12)); padding: 4px 8px; text-align: left; }
        .bubble th { background: rgba(0,0,0,0.04); font-weight: 500; }
        .pending-change { margin: 12px 0; border: 1px solid var(--primary-color); border-radius: 8px; overflow: hidden; }
        .pending-change .header { padding: 8px 12px; background: var(--primary-color); color: white; font-size: 13px; font-weight: 500; }
        .pending-change .diff { padding: 8px 12px; background: var(--code-background-color, #2d2d2d); color: #ccc; font-size: 12px; overflow-x: auto; white-space: pre; font-family: monospace; max-height: 200px; overflow-y: auto; }
        .pending-change .actions { display: flex; gap: 8px; padding: 8px 12px; border-top: 1px solid var(--divider-color, rgba(0,0,0,0.12)); }
        .pending-change .actions button { flex: 1; padding: 6px; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500; }
        .btn-apply { background: var(--primary-color); color: white; }
        .btn-reject { background: var(--error-color, #db4437); color: white; }
        .input-area { padding: 12px 20px; border-top: 1px solid var(--divider-color, rgba(0,0,0,0.12)); }
        .input-row { display: flex; gap: 8px; align-items: flex-end; }
        .input-row textarea { flex: 1; border: 1px solid var(--divider-color, rgba(0,0,0,0.12)); border-radius: 8px; padding: 10px 14px; font-size: 14px; resize: none; outline: none; font-family: inherit; min-height: 40px; max-height: 120px; background: var(--secondary-background-color, #f9f9f9); color: var(--primary-text-color); }
        .input-row textarea:focus { border-color: var(--primary-color); }
        .input-row button { padding: 8px 20px; background: var(--primary-color); color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; height: 40px; }
        .input-row button:disabled { opacity: 0.5; cursor: not-allowed; }
        .typing { display: flex; gap: 4px; padding: 4px 0; }
        .typing span { width: 6px; height: 6px; background: var(--secondary-text-color); border-radius: 50%; animation: pulse 1.4s infinite; }
        .typing span:nth-child(2) { animation-delay: 0.2s; }
        .typing span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes pulse { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }
        .status-bar { padding: 4px 20px; font-size: 11px; color: var(--secondary-text-color); text-align: center; }
        .scroll-btn { position: absolute; bottom: 80px; right: 24px; background: var(--primary-color); color: white; border: none; border-radius: 50%; width: 36px; height: 36px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.2); display: none; align-items: center; justify-content: center; font-size: 18px; z-index: 10; }
        .scroll-btn.visible { display: flex; }
        .tool-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; background: rgba(0,0,0,0.05); margin-right: 4px; }
        .tool-result { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
        .disconnect-banner { background: var(--error-color, #db4437); color: white; padding: 8px 16px; font-size: 13px; text-align: center; display: none; }
        .disconnect-banner.visible { display: block; }
        .disconnect-banner button { background: white; color: var(--error-color, #db4437); border: none; border-radius: 4px; padding: 2px 8px; cursor: pointer; margin-left: 8px; font-size: 12px; font-weight: 500; }
        .clear-all-btn { background: none; border: 1px solid var(--error-color, #db4437); color: var(--error-color, #db4437); border-radius: 4px; padding: 2px 6px; cursor: pointer; font-size: 11px; font-weight: 500; opacity: 0.7; }
        .clear-all-btn:hover { opacity: 1; background: var(--error-color, #db4437); color: white; }
      </style>
      <div class="sidebar">
        <div class="sidebar-header">
          <span>OpenCode Chat</span>
          <div>
            <button id="clearAllBtn" class="clear-all-btn">Clear All</button>
            <button id="newSessionBtn">+ New</button>
          </div>
        </div>
        <div class="session-list" id="sessionList"></div>
      </div>
      <div class="main">
        <div class="disconnect-banner" id="disconnectBanner">
          Connection lost. <button id="reconnectBtn">Reconnect</button>
        </div>
        <div class="chat-header">
          <span class="title" id="chatTitle">Select a session</span>
        </div>
        <div style="position:relative;flex:1;display:flex;flex-direction:column">
          <div class="messages" id="messagesContainer">
            <div class="empty-state" id="emptyState">
              <div class="icon">💬</div>
              <div>Start a conversation</div>
            </div>
          </div>
          <button class="scroll-btn" id="scrollBtn">↓</button>
        </div>
        <div class="status-bar" id="statusBar"></div>
        <div class="input-area">
          <div class="input-row">
            <textarea id="chatInput" placeholder="Ask something about your home..." rows="1"></textarea>
            <button id="sendBtn">Send</button>
          </div>
        </div>
      </div>
    `;
    this._bindEvents();
    this._loadMarked();
  }

  _bindEvents() {
    const shadow = this.shadowRoot;
    shadow.getElementById('newSessionBtn').addEventListener('click', () => this._createSession());
    shadow.getElementById('clearAllBtn').addEventListener('click', () => this._clearAllSessions());
    shadow.getElementById('sendBtn').addEventListener('click', () => this._sendMessage());
    shadow.getElementById('scrollBtn').addEventListener('click', () => this._scrollToBottom());
    shadow.getElementById('reconnectBtn').addEventListener('click', () => this._reconnect());
    const input = shadow.getElementById('chatInput');
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._sendMessage(); }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
    const container = shadow.getElementById('messagesContainer');
    container.addEventListener('scroll', () => {
      const btn = shadow.getElementById('scrollBtn');
      const dist = container.scrollHeight - container.clientHeight - container.scrollTop;
      btn.classList.toggle('visible', dist > 100);
    });
    this._monitorConnection();
  }

  _monitorConnection() {
    if (this._connectionCheckInterval) clearInterval(this._connectionCheckInterval);
    this._connectionCheckInterval = setInterval(() => {
      if (!this._hass?.connection?.connected) {
        this._showDisconnectBanner();
      } else {
        this._hideDisconnectBanner();
      }
    }, 5000);
  }

  _showDisconnectBanner() {
    this.shadowRoot.getElementById('disconnectBanner').classList.add('visible');
  }

  _hideDisconnectBanner() {
    this.shadowRoot.getElementById('disconnectBanner').classList.remove('visible');
  }

  _reconnect() {
    if (this._hass?.connection) {
      this._hass.connection.reconnect();
    }
    this._hideDisconnectBanner();
  }

  _callWS(type, data = {}) {
    return new Promise((resolve, reject) => {
      const msgId = ++this._msgId;
      this._hass.callWS({ type: `opencode_chat/${type}`, id: msgId, ...data })
        .then(resolve)
        .catch(reject);
    });
  }

  async _loadSessions() {
    try {
      const result = await this._callWS('list_sessions');
      this._sessions = result || [];
      this._renderSessionList();
      if (this._sessions.length > 0 && !this._activeSessionId) {
        this._selectSession(this._sessions[0].id);
      } else if (this._sessions.length === 0) {
        this._showEmpty();
      }
    } catch (e) { console.error('Failed to load sessions', e); }
  }

  async _createSession() {
    try {
      const session = await this._callWS('create_session');
      this._sessions.unshift({ id: session.id, title: session.title, updated_at: session.updated_at, message_count: 0, has_pending: false });
      this._renderSessionList();
      this._selectSession(session.id);
    } catch (e) { console.error('Failed to create session', e); }
  }

  async _deleteSession(sessionId, e) {
    e.stopPropagation();
    try {
      await this._callWS('delete_session', { session_id: sessionId });
      this._sessions = this._sessions.filter(s => s.id !== sessionId);
      this._renderSessionList();
      if (this._activeSessionId === sessionId) {
        this._activeSessionId = null;
        delete this._sessionData[sessionId];
        if (this._sessions.length > 0) {
          this._selectSession(this._sessions[0].id);
        } else {
          this._showEmpty();
        }
      }
    } catch (e) { console.error('Failed to delete session', e); }
  }

  async _clearAllSessions() {
    if (!confirm('Delete all chat sessions?')) return;
    for (const s of [...this._sessions]) {
      try { await this._callWS('delete_session', { session_id: s.id }); } catch (_) {}
    }
    this._sessions = [];
    this._sessionData = {};
    this._activeSessionId = null;
    this._renderSessionList();
    this._showEmpty();
  }

  async _selectSession(sessionId) {
    this._activeSessionId = sessionId;
    this._renderSessionList();
    try {
      const data = await this._callWS('get_session', { session_id: sessionId });
      this._sessionData[sessionId] = data;
      this._renderMessages();
    } catch (e) { console.error('Failed to load session', e); }
  }

  _showEmpty() {
    const shadow = this.shadowRoot;
    shadow.getElementById('chatTitle').textContent = 'No session selected';
    shadow.getElementById('messagesContainer').innerHTML = `
      <div class="empty-state">
        <div class="icon">💬</div>
        <div>Create a new chat to start</div>
      </div>`;
    shadow.getElementById('chatInput').disabled = true;
    shadow.getElementById('sendBtn').disabled = true;
  }

  _renderSessionList() {
    const list = this.shadowRoot.getElementById('sessionList');
    list.innerHTML = this._sessions.map(s => `
      <div class="session-item${s.id === this._activeSessionId ? ' active' : ''}" data-id="${s.id}">
        <span class="title">${this._escapeHtml(s.title)}</span>
        <button class="delete-btn" data-id="${s.id}">✕</button>
      </div>
    `).join('');
    list.querySelectorAll('.session-item').forEach(el => {
      el.addEventListener('click', () => this._selectSession(el.dataset.id));
    });
    list.querySelectorAll('.delete-btn').forEach(el => {
      el.addEventListener('click', (e) => this._deleteSession(el.dataset.id, e));
    });
  }

  _renderMessages() {
    const data = this._sessionData[this._activeSessionId];
    if (!data) return;
    const shadow = this.shadowRoot;
    shadow.getElementById('chatTitle').textContent = data.title || 'Untitled';
    const container = shadow.getElementById('messagesContainer');
    shadow.getElementById('emptyState')?.remove();
    shadow.getElementById('chatInput').disabled = false;
    shadow.getElementById('sendBtn').disabled = false;

    let html = '';
    for (const msg of data.messages || []) {
      html += this._renderMessage(msg);
    }
    for (const change of data.pending_changes || []) {
      if (change.status === 'pending') {
        html += this._renderPendingChange(change);
      }
    }
    container.innerHTML = html;
    this._scrollToBottom();
  }

  _renderMessage(msg) {
    if (msg.role === 'tool' || msg.role === 'system') return '';
    let text = '';
    let toolCalls = [];
    for (const block of msg.content || []) {
      if (block.type === 'text') text += block.text || '';
      if (block.type === 'tool_use') toolCalls.push(block);
    }
    if (!text && toolCalls.length === 0) return '';
    let html = '';
    if (text) {
      html += `<div class="message ${msg.role}"><div class="bubble">${this._renderMarkdown(text)}</div></div>`;
    }
    for (const tc of toolCalls) {
      html += `<div class="message tool"><div class="bubble"><span class="tool-badge">🔧 ${this._escapeHtml(tc.name)}</span></div></div>`;
    }
    return html;
  }

  _renderPendingChange(change) {
    return `
      <div class="pending-change" data-change-id="${change.id}">
        <div class="header">${this._escapeHtml(change.summary || change.kind)}</div>
        <div class="diff">${this._escapeHtml(change.diff || '')}</div>
        <div class="actions">
          <button class="btn-apply" data-change-id="${change.id}">✓ Apply</button>
          <button class="btn-reject" data-change-id="${change.id}">✗ Reject</button>
        </div>
      </div>`;
  }

  _appendAssistantMessage(text) {
    const container = this.shadowRoot.getElementById('messagesContainer');
    const typingEl = container.querySelector('.typing');
    if (typingEl) typingEl.remove();

    let bubble = container.querySelector('.message.assistant:last-child .bubble');
    if (!bubble) {
      container.insertAdjacentHTML('beforeend', `<div class="message assistant"><div class="bubble"></div></div>`);
      bubble = container.querySelector('.message.assistant:last-child .bubble');
    }
    bubble.innerHTML = this._renderMarkdown(text);
    this._scrollToBottom();
  }

  _appendToolCall(name) {
    const container = this.shadowRoot.getElementById('messagesContainer');
    container.insertAdjacentHTML('beforeend',
      `<div class="message tool"><div class="bubble"><span class="tool-badge">🔧 ${this._escapeHtml(name)}</span></div></div>`);
    this._scrollToBottom();
  }

  _appendToolResult(name) {
    const container = this.shadowRoot.getElementById('messagesContainer');
    container.insertAdjacentHTML('beforeend',
      `<div class="message tool"><div class="bubble"><span class="tool-result">✅ ${this._escapeHtml(name)} done</span></div></div>`);
    this._scrollToBottom();
  }

  _appendPendingChange(change) {
    const container = this.shadowRoot.getElementById('messagesContainer');
    container.insertAdjacentHTML('beforeend', this._renderPendingChange(change));
    this._bindChangeButtons();
    this._scrollToBottom();
  }

  _showTyping() {
    const container = this.shadowRoot.getElementById('messagesContainer');
    container.insertAdjacentHTML('beforeend',
      `<div class="message assistant"><div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div></div>`);
    this._scrollToBottom();
  }

  async _sendMessage() {
    if (this._streaming || !this._activeSessionId) return;
    const input = this.shadowRoot.getElementById('chatInput');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';

    const container = this.shadowRoot.getElementById('messagesContainer');
    container.insertAdjacentHTML('beforeend',
      `<div class="message user"><div class="bubble">${this._escapeHtml(text)}</div></div>`);
    this._showTyping();
    this._scrollToBottom();
    this._streaming = true;
    this._setInputState(false);

    const msgId = ++this._msgId;
    const subId = this._hass.connection.subscribe((event) => {
      if (event.event?.type === 'text_delta') {
        this._appendAssistantMessage(event.event.text);
      } else if (event.event?.type === 'tool_use_start') {
        this._appendToolCall(event.event.name);
      } else if (event.event?.type === 'tool_result') {
        this._appendToolResult(event.event.name);
      } else if (event.event?.type === 'pending_change') {
        this._appendPendingChange(event.event.change);
      } else if (event.event?.type === 'session_renamed') {
        this.shadowRoot.getElementById('chatTitle').textContent = event.event.title;
      } else if (event.event?.type === 'error') {
        this._appendAssistantMessage(`**Error:** ${event.event.error}`);
      } else if (event.event?.type === 'chat_complete' || event.event?.type === 'turn_complete') {
        this._streaming = false;
        this._setInputState(true);
        this._loadSessions();
        if (this._activeSessionId) this._selectSession(this._activeSessionId);
      }
    }, {
      type: 'opencode_chat/chat',
      session_id: this._activeSessionId,
      message: text,
      id: msgId,
    });

    this._pendingSubscriptions[msgId] = subId;

    try {
      const result = await this._callWS('chat', { session_id: this._activeSessionId, message: text });
      if (result?.done) {
        this._streaming = false;
        this._setInputState(true);
        this._loadSessions();
        if (this._activeSessionId) this._selectSession(this._activeSessionId);
      }
    } catch (e) {
      this._streaming = false;
      this._setInputState(true);
      console.error('Chat error', e);
      this._appendAssistantMessage(`**Error:** ${e.message}`);
    } finally {
      if (this._pendingSubscriptions[msgId]) {
        this._hass.connection.unsubscribe(this._pendingSubscriptions[msgId]);
        delete this._pendingSubscriptions[msgId];
      }
    }
  }

  _setInputState(enabled) {
    const shadow = this.shadowRoot;
    shadow.getElementById('chatInput').disabled = !enabled;
    shadow.getElementById('sendBtn').disabled = !enabled;
    shadow.getElementById('statusBar').textContent = enabled ? '' : 'Thinking...';
  }

  async _applyChange(changeId, btn) {
    btn.disabled = true;
    try {
      await this._callWS('apply_change', { session_id: this._activeSessionId, change_id: changeId });
      const changeEl = this.shadowRoot.querySelector(`.pending-change[data-change-id="${changeId}"]`);
      if (changeEl) {
        changeEl.innerHTML = '<div class="header" style="background:var(--success-color,#43a047)">✓ Applied</div>';
      }
    } catch (e) {
      btn.disabled = false;
      console.error('Apply failed', e);
      alert('Failed to apply: ' + e.message);
    }
  }

  async _rejectChange(changeId, btn) {
    btn.disabled = true;
    try {
      await this._callWS('reject_change', { session_id: this._activeSessionId, change_id: changeId });
      const changeEl = this.shadowRoot.querySelector(`.pending-change[data-change-id="${changeId}"]`);
      if (changeEl) {
        changeEl.innerHTML = '<div class="header" style="background:var(--error-color,#db4437)">✗ Rejected</div>';
      }
    } catch (e) {
      btn.disabled = false;
      console.error('Reject failed', e);
    }
  }

  _bindChangeButtons() {
    const shadow = this.shadowRoot;
    shadow.querySelectorAll('.btn-apply').forEach(btn => {
      btn.addEventListener('click', () => this._applyChange(btn.dataset.changeId, btn));
    });
    shadow.querySelectorAll('.btn-reject').forEach(btn => {
      btn.addEventListener('click', () => this._rejectChange(btn.dataset.changeId, btn));
    });
  }

  _renderMarkdown(text) {
    if (!text) return '';
    if (this._markedReady) {
      try {
        return window.marked.parse(text, { breaks: true, gfm: true });
      } catch { }
    }
    return this._simpleMarkdown(text);
  }

  _simpleMarkdown(text) {
    let html = this._escapeHtml(text);
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    html = html.replace(/\n/g, '<br>');
    return html;
  }

  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  _scrollToBottom() {
    requestAnimationFrame(() => {
      const container = this.shadowRoot.getElementById('messagesContainer');
      container.scrollTop = container.scrollHeight;
    });
  }
}

customElements.define('opencode-chat-panel', OpenCodeChatPanel);
