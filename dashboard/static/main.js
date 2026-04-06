/**
 * Shared JavaScript utilities for the Trading Dashboard.
 */

/**
 * Fetch wrapper that handles auth and JSON parsing.
 * @param {string} url - API endpoint URL
 * @param {object} options - fetch options (method, headers, body, etc.)
 * @returns {Promise<any>} Parsed JSON response
 */
async function fetchAPI(url, options = {}) {
    const defaults = {
        headers: {
            'Accept': 'application/json',
            ...(options.headers || {}),
        },
    };
    const merged = { ...defaults, ...options, headers: { ...defaults.headers, ...(options.headers || {}) } };
    const response = await fetch(url, merged);
    if (!response.ok) {
        const text = await response.text();
        throw new Error(`HTTP ${response.status}: ${text}`);
    }
    return response.json();
}

/**
 * Format a number as ARS currency.
 * @param {number} amount
 * @returns {string}
 */
function formatCurrency(amount) {
    if (amount === null || amount === undefined || isNaN(amount)) return '--';
    return new Intl.NumberFormat('es-AR', {
        style: 'currency',
        currency: 'ARS',
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
    }).format(amount);
}

/**
 * Format a number with locale formatting.
 * @param {number} value
 * @param {number} decimals
 * @returns {string}
 */
function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    return new Intl.NumberFormat('es-AR', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    }).format(value);
}

/**
 * Format a number as percentage.
 * @param {number} value - The percentage value (e.g. 5.2 for 5.2%)
 * @returns {string}
 */
function formatPercent(value) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    const sign = value >= 0 ? '+' : '';
    return sign + value.toFixed(2) + '%';
}

/**
 * Format an ISO datetime string to a short time representation.
 * @param {string} isoString
 * @returns {string}
 */
function formatTime(isoString) {
    if (!isoString) return '--';
    try {
        const d = new Date(isoString);
        return d.toLocaleString('es-AR', {
            day: '2-digit',
            month: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        });
    } catch (e) {
        return isoString;
    }
}

/**
 * Format uptime seconds to human readable string.
 * @param {number} seconds
 * @returns {string}
 */
function formatUptime(seconds) {
    if (!seconds && seconds !== 0) return '--';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const parts = [];
    if (d > 0) parts.push(d + 'd');
    if (h > 0) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
}

/**
 * Generate a status badge HTML class string.
 * @param {string} status - EXECUTED, PENDING, CANCELLED, REJECTED
 * @returns {string} Tailwind classes
 */
function statusBadgeClass(status) {
    switch (status) {
        case 'EXECUTED':  return 'bg-emerald-900 text-emerald-300';
        case 'PENDING':   return 'bg-blue-900 text-blue-300';
        case 'CANCELLED': return 'bg-slate-700 text-slate-400';
        case 'REJECTED':  return 'bg-red-900 text-red-300';
        default:          return 'bg-slate-700 text-slate-400';
    }
}
