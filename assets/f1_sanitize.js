/**
 * F1 Dashboard — Client-side patches
 *
 * 1. Data Sanitization: intercepta cualquier NaN / undefined / vacío
 *    que llegue al DOM en la columna de posición y lo reemplaza con
 *    el último valor conocido del buffer o un guion (–).
 *
 * 2. Sort-change guard: fingerprint de posiciones → ejecuta lógica
 *    pesada SOLO cuando el orden real cambia, no en cada tick.
 *
 * Corre después de cada reconciliación de React/Dash vía MutationObserver.
 */
(function () {
  'use strict';

  /* ── Buffer: último entero válido por row-id ─────────────────── */
  var _posCache = Object.create(null);

  /* ── 1. Sanitizador de posición ──────────────────────────────── */
  function sanitizePositions () {
    var cells = document.querySelectorAll('td.f1-pos');
    cells.forEach(function (cell) {
      var raw = cell.textContent.trim();
      var rowId = (cell.closest('tr') || {}).id || null;

      /* Valores inválidos a detectar */
      var invalid = (
        raw === 'NaN'       ||
        raw === 'undefined' ||
        raw === 'null'      ||
        raw === ''          ||
        isNaN(parseInt(raw, 10))
      );

      if (invalid) {
        /* Restaurar último valor conocido o mostrar guion */
        var last = rowId ? _posCache[rowId] : null;
        cell.textContent = (last != null) ? String(last) : '–';
        if (!last) cell.style.backgroundColor = 'transparent';
        return;
      }

      var n = parseInt(raw, 10);
      if (!Number.isInteger(n) || n <= 0) {
        cell.textContent = '–';
        cell.style.backgroundColor = 'transparent';
        return;
      }

      /* Valor limpio → cachear */
      if (rowId) _posCache[rowId] = n;
    });
  }

  /* ── 2. Sort-change guard ────────────────────────────────────── */
  var _lastFingerprint = '';

  function getFingerprint () {
    return Array.from(document.querySelectorAll('td.f1-pos'))
      .map(function (c) { return c.textContent.trim(); })
      .join(',');
  }

  function onDashUpdate () {
    sanitizePositions();

    var fp = getFingerprint();
    if (fp !== _lastFingerprint) {
      _lastFingerprint = fp;
      /* Marcar la tabla para que otras extensiones/DevTools puedan detectarlo */
      var tbl = document.querySelector('.f1-table');
      if (tbl) tbl.setAttribute('data-order-ts', Date.now());
    }
  }

  /* ── 3. MutationObserver con debounce de 1 frame ─────────────── */
  var _raf = null;
  var observer = new MutationObserver(function () {
    /* cancelAnimationFrame colapsa micro-mutaciones en un solo pase */
    if (_raf) cancelAnimationFrame(_raf);
    _raf = requestAnimationFrame(onDashUpdate);
  });

  observer.observe(document.body, { childList: true, subtree: true });

  /* Pase inicial */
  onDashUpdate();
})();
