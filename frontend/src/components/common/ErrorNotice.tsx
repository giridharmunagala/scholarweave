import { Icon } from './Icon';

interface ErrorNoticeProps {
  title?: string;
  message: string;
}

export function ErrorNotice({ title = 'Something went wrong', message }: ErrorNoticeProps) {
  return (
    <div className="error-notice" role="alert">
      <Icon name="alert" size={15} />
      <div className="notice-body">
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}
