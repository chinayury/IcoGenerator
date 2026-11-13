/* IcoGenerator 管理控制台
 *
 * 安全约定：
 * - 令牌只存内存（sessionStorage），不写 localStorage，避免长期驻留；
 * - 所有文本插入 DOM 均走 textContent，绝不使用 innerHTML 拼接，
 *   防止文件名等用户可控内容造成 XSS；
 * - 凭据输入框留空表示「不修改」，避免把脱敏后的掩码值回写覆盖真实密钥。
 */
'use strict';

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = 'icogen_admin_token';

let token = sessionStorage.getItem(TOKEN_KEY) || '';

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text || '';
  el.className = 'msg' + (kind ? ' ' + kind : '');
}

function headers(extra) {
  const h = Object.assign({}, extra || {});
  if (token) h['Authorization'] = 'Bearer ' + token;
  return h;
}

async function api(method, path, body) {
  const opts = { method, headers: headers(body ? { 'Content-Type': 'application/json' } : {}) };
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error('服务返回了非 JSON 内容（HTTP ' + resp.status + '）');
  }
  if (!resp.ok) {
    const hint = data.hint ? '（' + data.hint + '）' : '';
    throw new Error((data.error || '请求失败') + hint);
  }
  return data;
}

function unlockCards(unlocked) {
  ['storageCard', 'schedCard', 'listCard'].forEach((id) => {
    $(id).classList.toggle('locked', !unlocked);
  });
}

function fmtBytes(n) {
  if (!n && n !== 0) return '-';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}

const STATUS_TEXT = { pending: '待上传', uploaded: '已上传', failed: '上传失败' };

/* 版本号单一来源是后端 /api/health，页面不硬编码，发版不再漏改。 */
async function loadVersion() {
  try {
    const resp = await fetch('/api/health', { cache: 'no-store' });
    const data = await resp.json();
    if (data && data.version) {
      const badge = $('versionBadge');
      if (badge) badge.textContent = 'v' + data.version;
      document.title = 'IcoGenerator · 管理控制台 v' + data.version;
    }
  } catch (e) {
    /* 服务未就绪时保留占位，各卡片已有错误提示 */
  }
}
loadVersion();

function renderArtifacts(artifacts) {
  const tbody = $('artifactBody');
  tbody.textContent = '';   // 清空而非 innerHTML = ''

  if (!artifacts.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    td.className = 'empty';
    td.textContent = '暂无生成记录';
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  artifacts.forEach((item) => {
    const tr = document.createElement('tr');

    const name = document.createElement('td');
    name.textContent = item.filename;
    tr.appendChild(name);

    const size = document.createElement('td');
    size.textContent = fmtBytes(item.size);
    tr.appendChild(size);

    const sizes = document.createElement('td');
    sizes.textContent = (item.sizes && item.sizes.length) ? item.sizes.join(', ') : '-';
    tr.appendChild(sizes);

    const status = document.createElement('td');
    const tag = document.createElement('span');
    const key = STATUS_TEXT[item.upload_status] ? item.upload_status : 'pending';
    tag.className = 'tag ' + key;
    tag.textContent = STATUS_TEXT[item.upload_status] || item.upload_status;
    if (item.last_error) tag.title = item.last_error;
    status.appendChild(tag);
    tr.appendChild(status);

    const created = document.createElement('td');
    created.textContent = (item.created_at || '').replace('T', ' ').replace('+00:00', ' UTC');
    tr.appendChild(created);

    const actions = document.createElement('td');
    const dl = document.createElement('a');
    dl.className = 'btn small';
    dl.textContent = '下载';
    dl.href = '/api/artifacts/' + encodeURIComponent(item.artifact_id) + '/download';
    // 下载接口需要令牌，用 fetch 取回后由 Blob 触发保存
    dl.href = 'javascript:void(0)';
    dl.addEventListener('click', () => downloadArtifact(item.artifact_id, item.filename));
    actions.appendChild(dl);

    const up = document.createElement('button');
    up.className = 'btn small';
    up.textContent = '上传';
    up.addEventListener('click', async () => {
      up.disabled = true;
      try {
        const r = await api('POST', '/api/upload', { artifact_id: item.artifact_id });
        setMsg('schedMsg', r.message || '上传完成', r.ok ? 'ok' : 'warn');
        refreshList();
      } catch (e) {
        setMsg('schedMsg', e.message, 'err');
      } finally {
        up.disabled = false;
      }
    });
    actions.appendChild(up);
    tr.appendChild(actions);

    tbody.appendChild(tr);
  });
}

async function downloadArtifact(id, filename) {
  try {
    const resp = await fetch('/api/artifacts/' + encodeURIComponent(id) + '/download',
                             { headers: headers() });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('下载失败 HTTP ' + resp.status));
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'icon.ico';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    setMsg('schedMsg', e.message, 'err');
  }
}

async function refreshList() {
  try {
    const data = await api('GET', '/api/artifacts?limit=200');
    renderArtifacts(data.artifacts || []);
    const st = data.stats || {};
    $('statsLine').textContent =
      '共 ' + (st.count || 0) + ' 个文件 / ' + fmtBytes(st.total_bytes) +
      '｜本地目录：' + (st.root || '-');
  } catch (e) {
    setMsg('schedMsg', e.message, 'err');
  }
}

function fillForm(cfg, sched) {
  $('storageEnabled').checked = !!cfg.enabled;
  $('endpoint').value = cfg.endpoint || '';
  $('region').value = cfg.region || '';
  $('bucket').value = cfg.bucket || '';
  $('prefix').value = cfg.prefix || 'icongenerator';
  $('pathStyle').value = String(cfg.path_style !== false);
  $('useSsl').checked = cfg.use_ssl !== false;
  $('timeout').value = cfg.timeout || 30;
  // 凭据不回填：服务端只给掩码，回填会覆盖真实密钥
  $('accessKey').value = '';
  $('secretKey').value = '';
  $('accessKey').placeholder = cfg.access_key ? ('已配置（' + cfg.access_key + '）') : '未配置';

  $('schedEnabled').checked = !!sched.enabled;
  $('interval').value = sched.interval_minutes || 30;
  $('batchSize').value = sched.batch_size || 50;
  $('maxRetries').value = sched.max_retries != null ? sched.max_retries : 3;
}

async function loadConfig() {
  setMsg('tokenMsg', '连接中…');
  try {
    const data = await api('GET', '/api/config');
    fillForm(data.config || {}, data.scheduler || {});
    unlockCards(true);

    const sb = $('storageBadge');
    sb.textContent = '对象存储：' + (data.config.enabled ? '已启用' : '未启用');
    sb.classList.toggle('on', !!data.config.enabled);

    const scb = $('schedBadge');
    scb.textContent = '定时任务：' + (data.scheduler.enabled ? '已启用' : '未启用');
    scb.classList.toggle('on', !!data.scheduler.enabled);

    setMsg('tokenMsg', '连接成功，令牌有效。', 'ok');
    await refreshList();
  } catch (e) {
    unlockCards(false);
    setMsg('tokenMsg', e.message, 'err');
  }
}

function storagePayload() {
  return {
    enabled: $('storageEnabled').checked,
    endpoint: $('endpoint').value.trim(),
    region: $('region').value.trim(),
    bucket: $('bucket').value.trim(),
    prefix: $('prefix').value.trim(),
    path_style: $('pathStyle').value === 'true',
    use_ssl: $('useSsl').checked,
    timeout: parseInt($('timeout').value, 10) || 30,
    // 仅在用户真的输入时才提交；空串表示不修改
    access_key: $('accessKey').value.trim(),
    secret_key: $('secretKey').value.trim(),
  };
}

function schedulerPayload() {
  return {
    scheduler_enabled: $('schedEnabled').checked,
    interval_minutes: parseInt($('interval').value, 10) || 30,
    batch_size: parseInt($('batchSize').value, 10) || 50,
    max_retries: parseInt($('maxRetries').value, 10) || 0,
  };
}

// ---- 事件绑定 ----
$('btnLoad').addEventListener('click', () => {
  token = $('token').value.trim();
  if (!token) { setMsg('tokenMsg', '请输入令牌', 'warn'); return; }
  sessionStorage.setItem(TOKEN_KEY, token);
  loadConfig();
});

$('btnSaveStorage').addEventListener('click', async () => {
  try {
    const r = await api('POST', '/api/config', storagePayload());
    setMsg('storageMsg', r.message || '配置已保存', 'ok');
    fillForm(r.storage || {}, r.scheduler || {});
    await refreshList();
  } catch (e) {
    setMsg('storageMsg', e.message, 'err');
  }
});

$('btnTestStorage').addEventListener('click', async () => {
  setMsg('storageMsg', '测试中…');
  try {
    // 先保存再测，确保测试的是刚填的配置
    await api('POST', '/api/config', storagePayload());
    const r = await api('POST', '/api/storage/test', {});
    setMsg('storageMsg', r.message || (r.ok ? '连接正常' : '连接失败'), r.ok ? 'ok' : 'err');
  } catch (e) {
    setMsg('storageMsg', e.message, 'err');
  }
});

$('btnSaveSched').addEventListener('click', async () => {
  try {
    const r = await api('POST', '/api/config', schedulerPayload());
    setMsg('schedMsg', r.message || '调度配置已保存', 'ok');
    fillForm(r.storage || {}, r.scheduler || {});
  } catch (e) {
    setMsg('schedMsg', e.message, 'err');
  }
});

async function schedulerAction(action, label) {
  setMsg('schedMsg', label + '…');
  try {
    const r = await api('POST', '/api/scheduler', { action });
    setMsg('schedMsg', r.message || '操作完成', 'ok');
  } catch (e) {
    setMsg('schedMsg', e.message, 'err');
  }
}

$('btnSchedStart').addEventListener('click', () => schedulerAction('start', '正在启动'));
$('btnSchedStop').addEventListener('click', () => schedulerAction('stop', '正在停止'));

$('btnUploadPending').addEventListener('click', async () => {
  setMsg('schedMsg', '正在上传…');
  try {
    const r = await api('POST', '/api/upload/pending', {});
    setMsg('schedMsg', r.message || '上传完成', r.ok ? 'ok' : 'warn');
    await refreshList();
  } catch (e) {
    setMsg('schedMsg', e.message, 'err');
  }
});

$('btnRefresh').addEventListener('click', refreshList);

// 页面加载时若已有令牌则自动连接
if (token) {
  $('token').value = token;
  loadConfig();
} else {
  unlockCards(false);
}