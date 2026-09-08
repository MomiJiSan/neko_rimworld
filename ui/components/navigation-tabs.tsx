type TabItem = { id: string; label: any }

export function NavigationTabs(props: { id: string; activeId: string; items: TabItem[]; onChange: (id: string) => void; children?: any }) {
  function onKey(event: KeyboardEvent, index: number) {
    let next = index
    if (event.key === "ArrowRight") next = (index + 1) % props.items.length
    else if (event.key === "ArrowLeft") next = (index + props.items.length - 1) % props.items.length
    else if (event.key === "Home") next = 0
    else if (event.key === "End") next = props.items.length - 1
    else return
    event.preventDefault()
    const row = (event.currentTarget as HTMLElement).parentElement
    props.onChange(props.items[next].id)
    const button = row?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]
    button?.focus()
  }
  return <div className="neko-tabs">
    <div className="neko-tab-list" role="tablist" aria-label="页面导航">
      {props.items.map((item, index) => <button key={item.id} type="button" role="tab"
        id={`${props.id}-tab-${item.id}`} aria-controls={`${props.id}-panel-${item.id}`}
        aria-selected={item.id === props.activeId ? "true" : "false"} tabIndex={item.id === props.activeId ? 0 : -1}
        className={`neko-tab-button ${item.id === props.activeId ? "is-active" : ""}`}
        onClick={() => props.onChange(item.id)} onKeyDown={(event: KeyboardEvent) => onKey(event, index)}>{item.label}</button>)}
    </div>
    <div className="neko-tab-panel" role="tabpanel" id={`${props.id}-panel-${props.activeId}`} aria-labelledby={`${props.id}-tab-${props.activeId}`}>{props.children}</div>
  </div>
}
