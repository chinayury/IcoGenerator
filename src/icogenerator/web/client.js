/* 网页版 ICO 图标生成器
 *
 * 安全约定：
 * - 所有文件名等用户可控内容一律走 textContent / createElement，
 *   不使用 innerHTML 拼接，避免 XSS；
 * - 图片在浏览器内先做尺寸/像素上限检查，再决定是否上传给本地服务。
 */
'use strict';

const $ = (id) => document.getElementById(id);

const ALL_SIZES = [16, 24, 32, 48, 64, 128, 256];
const MAX_EDGE = 8192;
const MAX_PIXELS = 40e6; // 前端先挡一道，减少无效请求

let selected = new Set([16, 32, 48, 256]);
let currentFile = null;
let currentImage = null;   // ImageBitmap / HTMLImageElement
let mergedBlobUrl = null;
let bundleEntries = [];
let siteAssetsBlobUrl = null;
let bundleBlobUrl = null;
let includeMaskable = true;

/* ---------- 服务探测 ---------- */
async function checkServer() {
  try {
    const resp = await fetch('/api/health', { cache: 'no-store' });
    const data = await resp.json();
    setBadge('服务已就绪 v' + data.version, 'ok');
  } catch (e) {
    setBadge('未连接到本地服务', 'err');
  }
}

function setBadge(text, kind) {
  const el = $('statusBadge');
  el.textContent = text;
  el.className = 'badge' + (kind ? ' ' + kind : '');
}

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text || '';
  el.className = 'msg' + (kind ? ' ' + kind : '');
}

/* ---------- 尺寸选择 ---------- */
function buildSizes() {
  const box = $('sizes');
  box.textContent = '';
  ALL_SIZES.forEach((size) => {
    const label = document.createElement('label');
    label.className = 'size';

    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = selected.has(size);
    input.addEventListener('change', () => {
      if (input.checked) selected.add(size);
      else selected.delete(size);
      updateGenButton();
    });

    const text = document.createElement('span');
    text.textContent = size + '×' + size;

    label.appendChild(input);
    label.appendChild(text);
    box.appendChild(label);
  });
}

const PRESETS = {
  favicon: { label: 'favicon', sizes: [16, 32, 48] },
  windows: { label: 'Windows 程序', sizes: [16, 24, 32, 48, 64, 256] },
  full:    { label: '完整尺寸集', sizes: ALL_SIZES },
  small:   { label: '轻量', sizes: [16, 32] },
  mobile:  { label: '移动端 / PWA', sizes: [48, 64, 128, 256] },
};

function buildPresets() {
  const box = $('presets');
  box.textContent = '';
  Object.keys(PRESETS).forEach((key) => {
    const btn = document.createElement('button');
    btn.className = 'chip';
    btn.textContent = PRESETS[key].label;
    btn.addEventListener('click', () => {
      selected = new Set(PRESETS[key].sizes);
      buildSizes();
      updateGenButton();
      setMsg('genMsg', '已选择预设：' + PRESETS[key].label, 'ok');
    });
    box.appendChild(btn);
  });
}

function updateGenButton() {
  $('btnGen').disabled = !currentFile || selected.size === 0;
}

/* 分级提示（docs/09 要点 5.1）：
   服务端返回 hints[{level, code, message}]，前端按 level 决定样式，
   不匹配中文文案——文案可改，level 与 code 稳定。 */
function renderWarnings(data) {
  const box = $('warnings');
  box.textContent = '';
  if (!data) return;

  const add = (text, level) => {
    const line = document.createElement('p');
    line.className = 'hint ' + (level || 'warn');
    line.textContent = text;
    box.appendChild(line);
  };

  const hints = data.hints;
  if (hints && hints.length) {
    hints.forEach((hint) => add(hint.message, hint.level));
    return;
  }

  // 兜底：旧版服务端只回 analysis 字段
  const analysis = data.analysis || data;
  if (analysis.will_upscale) {
    add('源图较小，以下尺寸会被放大：' + analysis.upscaled_targets.join('、') + '，可能变模糊。');
  }
  if (analysis.opaque_source) {
    add('源图不含透明通道（常见于 JPEG），生成的 PNG 变体背景将不透明。');
  }
  if (analysis.cost) add(analysis.cost, 'info');
}

/* ---------- 选图 ---------- */
async function handleFile(file) {
  if (!file) return;
  setMsg('fileMsg', '');

  if (!/^image\//.test(file.type) && !/\.(jpe?g|png|gif|bmp|webp|tiff?|svg)$/i.test(file.name)) {
    setMsg('fileMsg', '请选择图片文件', 'err');
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    setMsg('fileMsg', '文件过大（上限 20MB）', 'err');
    return;
  }

  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => {
    const edge = Math.max(img.naturalWidth, img.naturalHeight);
    if (edge > MAX_EDGE || img.naturalWidth * img.naturalHeight > MAX_PIXELS) {
      URL.revokeObjectURL(url);
      setMsg('fileMsg', '图片尺寸过大（' + img.naturalWidth + '×' + img.naturalHeight + '）', 'err');
      return;
    }
    currentFile = file;
    currentImage = img;
    setMsg('fileMsg', '已选择：' + file.name + '（' + img.naturalWidth + '×' + img.naturalHeight + '）', 'ok');
    updateGenButton();
  };
  img.onerror = () => {
    URL.revokeObjectURL(url);
    setMsg('fileMsg', '无法解码该图片，请更换文件', 'err');
  };
  img.src = url;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000; // 分块避免 apply 参数过多爆栈
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/* ---------- 生成 ---------- */
async function generate() {
  if (!currentFile || selected.size === 0) return;

  const btn = $('btnGen');
  btn.disabled = true;
  setMsg('genMsg', '生成中…');

  try {
    const buffer = await currentFile.arrayBuffer();
    includeMaskable = $('includeMaskable').checked;
    const payload = {
      image_base64: arrayBufferToBase64(buffer),
      filename: currentFile.name,
      sizes: Array.from(selected).sort((a, b) => a - b),
      fit_mode: $('fit').value,
      output_name: 'favicon.ico',
      include_maskable: includeMaskable,
      safe_zone: $('safeZone').value,
      background: $('background').value,
      opaque_pad: $('padOpaque').checked,
      name: 'My App',
      theme_color: '#ffffff',
    };

    const resp = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || ('HTTP ' + resp.status));

    if (mergedBlobUrl) URL.revokeObjectURL(mergedBlobUrl);
    mergedBlobUrl = URL.createObjectURL(base64ToBlob(data.ico_base64, 'image/x-icon'));
    bundleEntries = [];

    renderPreview(data.sizes, includeMaskable);
    renderWarnings(data);
    refreshSiteAssetsButton(payload);
    $('resultCard').hidden = false;
    setMsg('genMsg', '生成成功：' + data.sizes.length + ' 个尺寸（' + data.size + ' 字节）', 'ok');
  } catch (e) {
    setMsg('genMsg', '生成失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
    updateGenButton();
  }
}

function base64ToBlob(b64, type) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: type });
}

/* 真实像素瓦片（docs/09 要点 6.1）：
   预览区为了看得清会放大，用户看不到 16px 下的真实观感。
   这里对 <= 48 的尺寸额外给一块 1:1 画布，配 image-rendering: pixelated，
   回答「缩到这么小还能认出吗」。 */
function renderActualTiles(sizes) {
  const box = $('actualTiles');
  if (!box) return;
  box.textContent = '';
  const small = sizes.filter((s) => s <= 48);
  if (!small.length || !currentImage) return;

  const head = document.createElement('small');
  head.className = 'actual-head';
  head.textContent = '实际像素（未放大）';
  box.appendChild(head);

  small.forEach((size) => {
    const cell = document.createElement('div');
    cell.className = 'tile actual';

    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true; // 与 Pillow LANCZOS 一致的缩小质量

    const w = currentImage.naturalWidth;
    const h = currentImage.naturalHeight;
    const side = Math.min(w, h);
    ctx.drawImage(currentImage, (w - side) / 2, (h - side) / 2, side, side, 0, 0, size, size);

    const label = document.createElement('small');
    label.textContent = size + '×' + size;

    cell.appendChild(canvas);
    cell.appendChild(label);
    box.appendChild(cell);
  });
}

function renderPreview(sizes, showMaskable = false) {
  const box = $('preview');
  box.textContent = '';
  if (!currentImage) return;

  renderActualTiles(sizes);

  const maxTile = 56;
  const items = showMaskable ? [...sizes, 'maskable'] : sizes;

  items.forEach((item) => {
    const cell = document.createElement('div');
    cell.className = 'tile';

    const canvas = document.createElement('canvas');
    const px = Math.min(typeof item === 'number' ? item : 256, maxTile);
    canvas.width = px;
    canvas.height = px;
    const ctx = canvas.getContext('2d');

    const w = currentImage.naturalWidth;
    const h = currentImage.naturalHeight;
    const side = Math.min(w, h);
    ctx.drawImage(currentImage, (w - side) / 2, (h - side) / 2, side, side, 0, 0, px, px);

    if (item === 'maskable') {
      ctx.strokeStyle = '#2563eb';
      ctx.lineWidth = 2;
      const inset = px * 0.1;
      ctx.strokeRect(inset, inset, px - inset * 2, px - inset * 2);
    }

    const label = document.createElement('small');
    label.textContent = item === 'maskable' ? 'maskable 安全区' : item + '×' + item;

    cell.appendChild(canvas);
    cell.appendChild(label);
    box.appendChild(cell);
  });
}

/* ---------- 导出 ---------- */
function triggerDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

$('btnSaveIco').addEventListener('click', () => {
  if (mergedBlobUrl) {
    triggerDownload(mergedBlobUrl, 'favicon.ico');
    setMsg('saveMsg', '已下载多尺寸 ICO', 'ok');
  }
});

$('btnSaveBundle').addEventListener('click', async () => {
  if (!currentFile || selected.size === 0) return;
  setMsg('saveMsg', '正在打包全部尺寸…');
  try {
    const buffer = await currentFile.arrayBuffer();
    const payload = {
      image_base64: arrayBufferToBase64(buffer),
      filename: currentFile.name,
      sizes: Array.from(selected).sort((a, b) => a - b),
      fit_mode: $('fit').value,
      safe_zone: Number($('safeZone') ? $('safeZone').value : 0.8) || 0.8,
    };
    const resp = await fetch('/api/bundle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => null);
      throw new Error((detail && detail.error) || ('HTTP ' + resp.status));
    }
    // 真正的 ZIP：由服务端用 zipfile 打包，产物与桌面版「保存全部尺寸」一致。
    const blob = await resp.blob();
    if (bundleBlobUrl) URL.revokeObjectURL(bundleBlobUrl);
    bundleBlobUrl = URL.createObjectURL(blob);
    triggerDownload(bundleBlobUrl, 'icons.zip');
    setMsg('saveMsg', '已下载全部尺寸 ZIP（每个尺寸一个独立 ICO）', 'ok');
  } catch (e) {
    setMsg('saveMsg', '导出失败：' + e.message, 'err');
  }
});

async function refreshSiteAssetsButton(payload) {
  const btn = $('btnSaveSiteAssets');
  if (!btn) return;
  btn.disabled = !mergedBlobUrl;
  btn.onclick = async () => {
    if (!payload) return;
    setMsg('saveMsg', '正在生成站点物料包…');
    try {
      const resp = await fetch('/api/site-assets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (resp.status === 401) throw new Error('需要管理员令牌，请使用桌面版或管理接口生成站点物料包');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const blob = await resp.blob();
      if (siteAssetsBlobUrl) URL.revokeObjectURL(siteAssetsBlobUrl);
      siteAssetsBlobUrl = URL.createObjectURL(blob);
      triggerDownload(siteAssetsBlobUrl, 'site-assets.zip');
      setMsg('saveMsg', '已下载站点物料包', 'ok');
    } catch (e) {
      setMsg('saveMsg', '站点物料包失败：' + e.message, 'err');
    }
  };
}

/* ---------- 事件绑定 ---------- */
$('drop').addEventListener('click', () => $('file').click());
$('drop').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') $('file').click();
});
$('file').addEventListener('change', (e) => handleFile(e.target.files[0]));

['dragenter', 'dragover'].forEach((evt) =>
  $('drop').addEventListener(evt, (e) => {
    e.preventDefault();
    $('drop').classList.add('over');
  })
);
['dragleave', 'drop'].forEach((evt) =>
  $('drop').addEventListener(evt, (e) => {
    e.preventDefault();
    $('drop').classList.remove('over');
  })
);
$('drop').addEventListener('drop', (e) => {
  if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});

$('btnGen').addEventListener('click', generate);

buildPresets();
buildSizes();
refreshSiteAssetsButton(null);
checkServer();