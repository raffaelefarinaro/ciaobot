export {}

document.body.replaceChildren()
const app = document.createElement('div')
app.id = 'app'
const opener = document.createElement('button')
opener.textContent = 'Open workspace terminal'
document.body.append(app, opener)
history.replaceState(null, '', '/settings/providers')
await import('../../../src/main')
const { useTerminalStore } = await import('../../../src/stores/terminal')
opener.addEventListener('click', () => { void useTerminalStore().openWorkspaceForSettings('personal') })
