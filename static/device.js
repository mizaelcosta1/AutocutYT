// Intentional fallback for browsers that reduce or omit device metadata.
(() => {
  function template() {
    const width = document.documentElement.clientWidth || innerWidth;
    const ua = navigator.userAgent || '';
    const touch = navigator.maxTouchPoints > 0;
    const tablet = /iPad|Tablet|Android(?!.*Mobile)/i.test(ua) || (/Macintosh/i.test(ua) && touch);
    const phone = navigator.userAgentData?.mobile || /iPhone|Android.*Mobile/i.test(ua);
    const compact = width < 680 || (phone && !tablet && width < 1000);
    document.documentElement.dataset.device = compact ? 'phone' : tablet || width < 1120 ? 'tablet' : 'desktop';
  }
  template();
  addEventListener('resize', template, {passive:true});
})();
document.addEventListener('DOMContentLoaded', () => {
  const grid = document.querySelector('.source-grid');
  if (!grid) return;
  grid.dataset.source = 'file';
  const choices = document.querySelectorAll('[data-source-choice]');
  choices.forEach(button => button.addEventListener('click', () => {
    grid.dataset.source = button.dataset.sourceChoice;
    choices.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  }));
});
