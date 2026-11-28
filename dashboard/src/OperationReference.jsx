import React from 'react';

export function OperationNumber({number}) {
  return number ? <code className="learning-operation-number">{number}</code> : null;
}

export function OperationLink({eventId,number,onOpenEvent}) {
  return <a className="learning-source-link" href={'#learning-event-'+encodeURIComponent(eventId)}
    onClick={event=>onOpenEvent(event,eventId)}>
    <span>View original record</span><OperationNumber number={number}/>
  </a>;
}
