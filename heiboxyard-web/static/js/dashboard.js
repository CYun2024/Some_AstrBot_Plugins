// Dashboard-specific interactions
// Most functionality is handled by common.js and inline handlers

// Animate stat numbers on load
document.addEventListener('DOMContentLoaded', () => {
    const statValues = document.querySelectorAll('.stat-value');
    statValues.forEach(el => {
        const finalValue = parseInt(el.textContent) || 0;
        if (finalValue > 0) {
            el.textContent = '0';
            let current = 0;
            const step = Math.max(1, Math.floor(finalValue / 20));
            const interval = setInterval(() => {
                current += step;
                if (current >= finalValue) {
                    current = finalValue;
                    clearInterval(interval);
                }
                el.textContent = current;
            }, 30);
        }
    });
});
