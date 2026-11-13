/*
 * JupyDo notifications: a bar at the top of JupyterLab, updated live.
 *
 * Every 30 s it reads <base_url>/custom/notifications.json (written by the hub for
 * this environment: the user's unread notifications) and shows them in a bar above the
 * whole interface (full width). "Dismiss" marks the message as read on the hub (same as the hub home
 * page) without leaving JupyterLab. The disk-limit warning cannot be dismissed:
 * it disappears when the user is back under the limit.
 *
 * It also adds a "Hub Control Panel" button on the right of the menu bar (same as
 * File > Hub Control Panel), to go back to the hub home without using the menus,
 * and keeps the same light/dark theme in JupyterLab, the hub and the integrated apps (both ways) (light or
 * dark): they read it from the browser's storage, same site as JupyterLab.
 * Next to the button it shows until when the user's account is valid (red during
 * the last week), from <base_url>/custom/account.json.
 */
import { IThemeManager } from '@jupyterlab/apputils';
import { PageConfig, URLExt } from '@jupyterlab/coreutils';
import { Widget } from '@lumino/widgets';

const POLL_MS = 30000;
// where the hub (and JupyDo pages) and the integrated apps keep their light/dark choice
const THEME_KEYS = ['jupyterhub-bs-theme', 'gis-mode'];
const COLORS = { danger: '#c62828', warning: '#e65100', info: '#1565c0' };

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

// JupyterLab themes for the hub's light/dark choice
const LAB_THEMES = { light: 'JupyterLab Light', dark: 'JupyterLab Dark' };

function syncTheme(themeManager) {
  // hub -> JupyterLab: the choice made in the hub (or in another JupyterLab tab) wins
  // at start, and is followed live when it changes there
  const applyHubTheme = () => {
    let mode = null;
    try {
      mode = localStorage.getItem(THEME_KEYS[0]);
    } catch (e) {
      return;
    }
    if (!LAB_THEMES[mode] || !themeManager.theme) {
      return;
    }
    const current = themeManager.isLight(themeManager.theme) ? 'light' : 'dark';
    if (current !== mode) {
      themeManager.setTheme(LAB_THEMES[mode]);
    }
  };
  window.addEventListener('storage', event => {
    if (event.key === THEME_KEYS[0]) {
      applyHubTheme();
    }
  });
  applyHubTheme();

  // JupyterLab -> hub and apps
  const sync = () => {
    if (!themeManager.theme) {
      return;
    }
    const mode = themeManager.isLight(themeManager.theme) ? 'light' : 'dark';
    for (const key of THEME_KEYS) {
      try {
        localStorage.setItem(key, mode);
      } catch (e) {
        // storage disabled: nothing to sync
      }
    }
  };
  themeManager.themeChanged.connect(sync);
  sync();
}

function activate(app, themeManager) {
  if (themeManager) {
    app.restored.then(() => syncTheme(themeManager));
  }

  // Full-width bar fixed above the whole JupyterLab interface; the interface is
  // moved down by the bar's height, so nothing (menu, tabs) is covered.
  const bar = document.createElement('div');
  bar.id = 'jupydo-notifications';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:10000;display:none;' +
                      'box-shadow:0 2px 6px rgba(0,0,0,.35)';
  document.body.appendChild(bar);

  const dismissed = new Set();
  const fileUrl = URLExt.join(PageConfig.getBaseUrl(), 'custom', 'notifications.json');
  const hubPrefix = PageConfig.getOption('hubPrefix') || '/hub/';
  let lastHeight = -1;

  function layout() {
    const height = bar.style.display === 'none' ? 0 : bar.offsetHeight;
    if (height === lastHeight) {
      return;
    }
    lastHeight = height;
    app.shell.node.style.top = `${height}px`;
    window.dispatchEvent(new Event('resize'));  // JupyterLab re-fits its layout
  }

  function render(messages) {
    const visible = messages.filter(m => !dismissed.has(m.id));
    if (!visible.length) {
      bar.style.display = 'none';
      bar.innerHTML = '';
    } else {
      bar.innerHTML = visible.map(m => `
        <div style="display:flex;align-items:center;gap:16px;padding:10px 18px;color:#fff;
                    font-size:16px;font-weight:700;line-height:1.35;
                    border-bottom:1px solid rgba(255,255,255,.35);
                    background:${COLORS[m.level] || COLORS.info}">
          <span style="font-size:22px">${m.level === 'info' ? '&#8505;' : '&#9888;'}</span>
          <span style="flex:1">${escapeHtml(m.text)}
            <span style="font-weight:400;font-size:13px;opacity:.9"> &mdash; from ${escapeHtml(m.from)}</span></span>
          ${m.link ? `<a href="${escapeHtml(m.link)}" target="_blank" rel="noopener"
               style="background:#fff;color:#222;font-weight:700;font-size:14px;border-radius:4px;
               padding:6px 16px;text-decoration:none">Open</a>` : ''}
          ${m.id.startsWith('disk-') ? '' :
            `<button data-id="${escapeHtml(m.id)}" style="background:#fff;color:#222;font-weight:700;
               font-size:14px;border:0;border-radius:4px;padding:6px 16px;cursor:pointer">Dismiss</button>`}
        </div>`).join('');
      bar.querySelectorAll('button[data-id]').forEach(b => {
        b.onclick = () => dismiss(b.dataset.id, messages);
      });
      bar.style.display = 'block';
    }
    layout();
  }

  async function dismiss(id, messages) {
    dismissed.add(id);
    render(messages);
    try {
      await fetch(URLExt.join(hubPrefix, 'jupydo', 'notifications', id, 'read'),
                  { credentials: 'same-origin', redirect: 'manual' });
    } catch (e) {
      console.warn('JupyDo: could not dismiss notification', e);
    }
  }

  async function poll() {
    try {
      const response = await fetch(`${fileUrl}?t=${Date.now()}`,
                                   { cache: 'no-store', credentials: 'same-origin' });
      render(response.ok ? await response.json() : []);
    } catch (e) {
      // no notifications file in this environment: nothing to show
    }
  }

  const accountLabel = addAccountLabel(app);
  const accountUrl = URLExt.join(PageConfig.getBaseUrl(), 'custom', 'account.json');
  async function pollAccount() {
    try {
      const response = await fetch(`${accountUrl}?t=${Date.now()}`, { cache: 'no-store', credentials: 'same-origin' });
      showAccount(accountLabel, response.ok ? await response.json() : {});
    } catch (e) {
      showAccount(accountLabel, {});
    }
  }

  app.restored.then(poll);
  app.restored.then(pollAccount);
  setInterval(poll, POLL_MS);
  setInterval(pollAccount, POLL_MS);

  addHubButton(app, hubPrefix);
}

function addAccountLabel(app) {
  const widget = new Widget();
  widget.id = 'jupydo-account-validity';
  widget.node.style.cssText = 'display:none;align-items:center;padding:0 8px;font-size:12px;white-space:nowrap';
  app.shell.add(widget, 'top', { rank: 1990 });
  return widget;
}

function showAccount(widget, account) {
  if (!account || !account.until) {
    widget.node.style.display = 'none';
    return;
  }
  const left = account.days_left;
  const urgent = left <= 7;
  widget.node.textContent = `Account valid until ${account.until} (` +
    (left <= 0 ? 'last day' : left === 1 ? '1 day left' : `${left} days left`) + ')';
  widget.node.style.display = 'flex';
  widget.node.style.color = urgent ? '#c62828' : 'var(--jp-ui-font-color2)';
  widget.node.style.fontWeight = urgent ? '700' : '400';
}

function addHubButton(app, hubPrefix) {
  const widget = new Widget();
  widget.id = 'jupydo-hub-button';
  widget.node.style.cssText = 'display:flex;align-items:center;padding:0 8px';
  const button = document.createElement('button');
  button.className = 'jp-mod-styled jp-mod-accept';
  button.style.cssText = 'height:22px;line-height:20px;padding:0 10px;font-size:12px';
  button.textContent = 'Hub Control Panel';
  button.title = 'Go to the hub home: environments, services, notifications';
  // same command as File > Hub Control Panel when available, else open the hub home
  button.onclick = () => app.commands.hasCommand('hub:control-panel')
    ? app.commands.execute('hub:control-panel')
    : window.open(URLExt.join(hubPrefix, 'home'), '_blank');
  widget.node.appendChild(button);
  app.shell.add(widget, 'top', { rank: 2000 });
}

export default {
  id: 'jupydo-notifications:plugin',
  description: 'JupyDo notifications bar, account validity, Hub Control Panel button, theme sync',
  autoStart: true,
  optional: [IThemeManager],
  activate
};
