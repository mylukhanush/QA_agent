/**
 * QA Agent — Frontend JavaScript
 * Handles HTMX events, toast notifications, and shared utilities.
 */

// ── Toast Notifications ──────────────────────────────────────────

function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        toast.style.transition = 'all 300ms ease';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ── HTMX Event Hooks ─────────────────────────────────────────────

document.addEventListener('htmx:afterSwap', (evt) => {
    // Apply fade-in animation to newly swapped content
    const target = evt.detail.target;
    if (target) {
        target.classList.add('fade-in');
    }
});

document.addEventListener('htmx:responseError', (evt) => {
    showToast('Request failed. Please try again.', 'error');
});

// ── Utility: Time Ago ────────────────────────────────────────────

function timeAgo(dateString) {
    const date = new Date(dateString);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
}

// ── Utility: Format Duration ─────────────────────────────────────

function formatDuration(ms) {
    if (!ms) return '—';
    if (ms < 1000) return ms + 'ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
    return (ms / 60000).toFixed(1) + 'm';
}

// ── Keyboard Shortcuts ───────────────────────────────────────────

document.addEventListener('keydown', (e) => {
    // Ctrl+K or Cmd+K — focus search/situation input
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        const input = document.getElementById('situation-input');
        if (input) input.focus();
    }
});

// ── Auto-update "time ago" elements ──────────────────────────────

function updateTimeAgo() {
    document.querySelectorAll('[data-time-ago]').forEach(el => {
        el.textContent = timeAgo(el.dataset.timeAgo);
    });
}

// Update every 30 seconds
setInterval(updateTimeAgo, 30000);

// ── Global Test Case Inspector ───────────────────────────────────

async function openTestCaseDetailsModal(tcId) {
    const modal = document.getElementById('test-case-details-modal');
    if (!modal) return;

    const title = document.getElementById('tc-details-title');
    const category = document.getElementById('tc-details-category');
    const desc = document.getElementById('tc-details-desc');
    const stepsCont = document.getElementById('tc-details-steps');
    const date = document.getElementById('tc-details-date');
    const runBtn = document.getElementById('tc-details-run-btn');

    // Reset view
    title.innerText = 'Loading...';
    stepsCont.innerHTML = '<div class="py-12 flex justify-center"><div class="w-8 h-8 border-2 border-brand-500/20 border-t-brand-500 rounded-full animate-spin"></div></div>';
    modal.classList.remove('hidden');
    
    try {
        const resp = await fetch(`/api/test-cases/${tcId}`);
        const data = await resp.json();

        title.innerText = data.name || 'Test Case Detail';
        category.innerText = data.category;
        desc.innerText = data.situation_description;
        date.innerText = data.created_at || '—';
        
        runBtn.onclick = () => window.location.href = `/run?tc_id=${data.id}`;

        if (data.steps && data.steps.length > 0) {
            stepsCont.innerHTML = data.steps.map((s, idx) => `
                <div class="flex items-start gap-4 p-3 bg-surface-900/30 border border-white/5 rounded-xl">
                    <span class="text-[10px] font-mono text-gray-600 mt-0.5">${idx + 1}</span>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2 mb-0.5">
                            <span class="text-xs font-bold text-brand-400 uppercase tracking-tighter">${s.action}</span>
                            <span class="text-gray-700">·</span>
                            <span class="text-xs text-gray-300 font-medium truncate">${s.target || ''}</span>
                        </div>
                        <p class="text-[11px] text-gray-500 italic">${s.description || ''}</p>
                    </div>
                </div>
            `).join('');
        } else {
            stepsCont.innerHTML = '<div class="text-center py-8 text-gray-600 text-xs italic">No steps generated yet.</div>';
        }
    } catch (err) {
        console.error('Failed to load test case:', err);
        title.innerText = 'Error Loading Details';
    }
}

function closeTestCaseDetailsModal() {
    const modal = document.getElementById('test-case-details-modal');
    if (modal) modal.classList.add('hidden');
}
