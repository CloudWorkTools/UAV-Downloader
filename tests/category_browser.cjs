const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.attributes={}; this.listeners={}; this.classes=new Set(); this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key)}; }
  appendChild(child) {this.children.push(child);}
  replaceChildren() {this.children=[];}
  setAttribute(key,value) {this.attributes[key]=value;}
  addEventListener(event,fn) {this.listeners[event]=fn;}
}
const context=vm.createContext({window:{},document:{createElement:tag=>new Element(tag)}});
vm.runInContext(fs.readFileSync('uav-web/frontend/category-browser.js','utf8'),context);
const container=new Element('div');
const selected=[];
const view=context.window.createCategoryBrowser(container,item=>selected.push(item));
view.render([{name:'衣著',items:[{name:'制服',url:'https://example.com/tags/uniform/'},{name:'其他',url:'https://example.com/tags/other/'}]}, {name:'空分類',items:[]}], 'https://example.com/tags/uniform/');
assert.equal(container.children.length,1);
assert.equal(container.children[0].tagName,'details');
assert.equal(container.children[0].children[0].textContent,'衣著 (2)');
const [first,second]=container.children[0].children[1].children;
assert.equal(first.attributes['aria-pressed'],'true');
second.listeners.click();
assert.equal(first.attributes['aria-pressed'],'false');
assert.equal(second.attributes['aria-pressed'],'true');
assert.equal(selected[0].url,'https://example.com/tags/other/');
view.render([{name:'新站分類',items:[{name:'新項目',url:'https://example.net/'}]}],'');
assert.equal(container.children.length,1);
assert.equal(container.children[0].children[0].textContent,'新站分類 (1)');
view.select('https://example.net/');
assert.equal(container.children[0].children[1].children[0].attributes['aria-pressed'],'true');
console.log('Expandable category groups, selection and site reset: PASS');
