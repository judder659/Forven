export function dispatchNotice(response: {
 research_deferred?: boolean;
 task?: {task_id: number | null; error?: unknown} | null;
 already_running?: boolean;
}): {state: string; message: string; failed: boolean} {
 if (response.research_deferred) return {state:'deferred',message:'Saved. Research is paused in manual mode. Start research from this Crucible when ready.',failed:false};
 if (response.task?.task_id && !response.task.error) return {state:'queued',message:response.already_running ? 'Research is already queued.' : 'Research queued.',failed:false};
 return {state:'dispatch_failed',message:'Saved, but research was not queued. Retry research from this Crucible; do not create it again.',failed:true};
}

/** Trap keyboard focus while mounted and return it to the invoking control. */
export function dialogFocus(node: HTMLElement) {
 const previous = document.activeElement as HTMLElement | null;
 const selector = 'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], summary, [tabindex="0"]';
 const controls = () => [...node.querySelectorAll<HTMLElement>(selector)].filter(el => el.getClientRects().length > 0);
 queueMicrotask(() => controls()[0]?.focus());
 const keydown = (event: KeyboardEvent) => {
  if (event.key !== 'Tab') return;
  const items = controls(); const first = items[0]; const last = items.at(-1);
  if (!first) {event.preventDefault(); return;}
  if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {event.preventDefault(); last?.focus();}
  else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {event.preventDefault(); first.focus();}
 };
 node.addEventListener('keydown',keydown);
 return {destroy() {node.removeEventListener('keydown',keydown); previous?.focus();}};
}

export function intakeKeys() {
 const keys = new Map<string,string>();
 return (payload: unknown): string => {
  const fingerprint = JSON.stringify(payload);
  let key = keys.get(fingerprint);
  if (!key) { key = crypto.randomUUID(); keys.set(fingerprint,key); }
  return key;
 };
}
