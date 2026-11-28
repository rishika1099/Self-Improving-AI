export function renderMarkdown(t){
  if(!t)return"";
  const inline=text=>text.replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/\*(.+?)\*/g,"<em>$1</em>");
  const lines=t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\r\n?/g,"\n").split("\n");
  let html="",listTag="",paragraph=[];
  const closeList=()=>{if(listTag){html+=`</${listTag}>`;listTag="";}};
  const closeParagraph=()=>{if(paragraph.length){html+=`<p>${paragraph.map(inline).join("<br/>")}</p>`;paragraph=[];}};
  for(const line of lines){
    const heading=line.match(/^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/);
    const divider=/^ {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(line);
    const item=line.match(/^ {0,3}(?:([-+*])|\d+[.)])\s+(.+)$/);
    if(!line.trim()||heading||divider){
      closeParagraph();closeList();
      if(heading)html+=`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
      else if(divider)html+="<hr/>";
    }else if(item){
      closeParagraph();
      const nextTag=item[1]?"ul":"ol";
      if(listTag!==nextTag){closeList();html+=`<${nextTag}>`;listTag=nextTag;}
      html+=`<li>${inline(item[2])}</li>`;
    }else{
      closeList();paragraph.push(line);
    }
  }
  closeParagraph();closeList();
  return html;
}
