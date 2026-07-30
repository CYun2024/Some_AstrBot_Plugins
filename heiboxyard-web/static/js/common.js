// ========== Sidebar Toggle ==========
const sidebar = document.getElementById('sidebar');
const sidebarOverlay = document.getElementById('sidebarOverlay');
const mobileMenuBtn = document.getElementById('mobileMenuBtn');
const sidebarToggle = document.getElementById('sidebarToggle');

function openSidebar() {
    sidebar.classList.add('open');
    sidebarOverlay.classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeSidebar() {
    sidebar.classList.remove('open');
    sidebarOverlay.classList.remove('active');
    document.body.style.overflow = '';
}

if (mobileMenuBtn) mobileMenuBtn.addEventListener('click', openSidebar);
if (sidebarToggle) sidebarToggle.addEventListener('click', closeSidebar);
if (sidebarOverlay) sidebarOverlay.addEventListener('click', closeSidebar);

// ========== Toast ==========
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => toast.remove(), 3000);
}

// ========== Modal ==========
const modalOverlay = document.getElementById('modalOverlay');
const modalTitle = document.getElementById('modalTitle');
const modalBody = document.getElementById('modalBody');
const modalConfirm = document.getElementById('modalConfirm');
const modalCancel = document.getElementById('modalCancel');
const modalClose = document.getElementById('modalClose');

let modalCallback = null;

function showModal(title, content, onConfirm) {
    modalTitle.textContent = title;
    modalBody.innerHTML = content;
    modalOverlay.classList.add('active');
    modalCallback = onConfirm;
}

function closeModal() {
    modalOverlay.classList.remove('active');
    modalCallback = null;
}

if (modalClose) modalClose.addEventListener('click', closeModal);
if (modalCancel) modalCancel.addEventListener('click', closeModal);
if (modalOverlay) {
    modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) closeModal();
    });
}
if (modalConfirm) {
    modalConfirm.addEventListener('click', async () => {
        if (modalCallback) {
            const result = await modalCallback();
            if (result !== false) closeModal();
        } else {
            closeModal();
        }
    });
}

// ========== Loading ==========
function showLoading(text = '处理中...') {
    const overlay = document.getElementById('loadingOverlay');
    const textEl = overlay.querySelector('.loading-text');
    if (textEl) textEl.textContent = text;
    overlay.classList.add('active');
}

function hideLoading() {
    const overlay = document.getElementById('loadingOverlay');
    overlay.classList.remove('active');
}

// ========== Refresh Button ==========
const refreshBtn = document.getElementById('refreshBtn');
if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
        refreshBtn.style.transform = 'rotate(360deg)';
        refreshBtn.style.transition = 'transform 0.5s ease';
        setTimeout(() => {
            refreshBtn.style.transform = '';
            refreshBtn.style.transition = '';
        }, 500);
        // 优先使用仪表盘的更新函数，否则强制刷新页面
        if (typeof window.updateStats === 'function') {
            window.updateStats();
        } else {
            location.reload(true);
        }
    });
}

// ========== Auto-refresh current window badge ==========
async function updateCurrentWindow() {
    try {
        const res = await fetch('/yard/admin/api/stats');
        const data = await res.json();
        const badge = document.getElementById('currentWindow');
        if (badge && data.current_window) {
            badge.textContent = data.current_window;
        }
    } catch (e) {
        // silently fail
    }
}
setInterval(updateCurrentWindow, 60000);

// ========== 在线人数更新 ==========
async function updateOnlineCount() {
    try {
        const res = await fetch('/yard/admin/api/online');
        const data = await res.json();
        const count = data.online || 0;
        const countEl = document.getElementById('onlineCount');
        const dotEl = document.getElementById('onlineDot');
        if (countEl) countEl.textContent = count;
        if (dotEl) {
            // 多人同时在线时显示黄色，否则绿色
            dotEl.style.color = count > 1 ? '#facc15' : '#22c55e';
        }
    } catch (e) {
        // 静默失败，不影响其他功能
    }
}

// 页面加载后首次更新，然后每 10 秒轮询
document.addEventListener('DOMContentLoaded', function() {
    updateOnlineCount();
    setInterval(updateOnlineCount, 10000);
});
