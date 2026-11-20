import { useEffect, useRef } from 'preact/hooks';
import type { ComponentChildren } from 'preact';

export function Modal({ title, children, busy, onClose }: {
  title: string; children: ComponentChildren; busy: boolean; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return <dialog ref={dialog} aria-labelledby="modal-title" onCancel={event => {
    event.preventDefault();
    if (!busy) onClose();
  }}>
    <div class="modal-content">
      <h2 id="modal-title">{title}</h2>
      {children}
    </div>
  </dialog>;
}
