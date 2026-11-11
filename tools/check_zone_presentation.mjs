import assert from 'node:assert/strict';
import {presentZones, zoneAt, zoneBadge, zoneBoundaryText, zoneLabel, zoneLegend, zoneMissing, zoneRows, zoneScale, zoneTable, zoneTitle, zoneToken} from '../src/vibesynergy/portal/static/js/zone_presentation.js';
const rows = Array.from({length:7},(_,i)=>({kind:'power',zone:i+1,low:100+i*50,seconds:60}));
assert.equal(new Set(rows.map(z=>zoneToken(z.zone))).size,7);
assert.equal(zoneToken('7'),'zone-7');
assert.equal(zoneToken('1; color:red'),'zone-other');
const scale=zoneScale(rows,'power');
assert.equal(zoneAt(scale,99),null);
assert.equal(zoneAt(scale,100),1);
assert.equal(zoneAt(scale,149.99),1);
assert.equal(zoneAt(scale,150),2);
assert.equal(zoneAt(scale,700),7);
assert.equal(zoneAt(scale,null),null);
assert.equal(zoneAt(scale,NaN),null);
assert.deepEqual(zoneScale(rows.filter(z=>z.zone!==4),'power'),[]);
assert.deepEqual(zoneScale([...rows,rows[0]],'power'),[]);
assert.deepEqual(zoneScale(rows.map(z=>({...z,low:null})),'power'),[]);
assert.deepEqual(zoneScale(rows,'pace'),[]);
assert.deepEqual(zoneScale([{kind:'hr',zone:1,low:100}],'hr'),[]);
assert.equal(zoneScale([...rows].reverse(),'power')[0].zone,1);
const html=zoneRows([rows[6],rows[0]],{kind:'power',boundaries:true});
assert.ok(html.indexOf('Zone 1')<html.indexOf('Zone 7'));
assert.ok(html.indexOf('>Z1<')<html.indexOf('>Z7<'));
assert.match(html,/var\(--zone-7\)/);
assert.match(html,/zone-badge/);
assert.match(html,/≥ 400 W/);
assert.match(html,/50%/);
const presented=presentZones(rows,{kind:'power'});
assert.equal(presented.zones.length,7);
assert.equal(new Set(presented.zones.map(z=>z.token)).size,7);
assert.deepEqual(presented.zones.map(z=>z.label),rows.map(z=>zoneLabel(z.zone)));
assert.equal(zoneBadge(null),'');
assert.equal(zoneBadge(undefined),'');
assert.equal(zoneToken(8),'zone-other');
assert.match(zoneBadge(8),/zone-other/);
assert.match(zoneBadge(8),/Z8/);
assert.doesNotMatch(zoneBadge(8),/zone-1/);
const table=zoneTable(rows,{kind:'power',boundaries:true});
assert.equal((table.match(/zone-badge/g)||[]).length,7);
assert.match(table,/Boundary/);
assert.match(table,/≥ 100 W/);
assert.doesNotMatch(zoneTable([{zone:1,seconds:60}],{kind:'hr',boundaries:false}),/Boundary/);
const legend=zoneLegend(scale,{kind:'power',title:'Power',unit:'W'});
assert.match(legend,/Power \(W\)/);
assert.match(legend,/var\(--zone-1\)/);
assert.match(legend,/≥ 100 W/);
const missing=zoneMissing('Heart rate');
assert.match(missing,/Zones not configured/);
assert.match(missing,/Heart rate/);
assert.match(missing,/settings\/data\/sources/);
assert.doesNotMatch(missing,/--s\d/);
assert.doesNotMatch(zoneRows([{zone:1,seconds:NaN}]),/NaN/);
assert.doesNotMatch(zoneRows([{zone:1,seconds:-5}]),/-5/);

// Pace: real device-recorded zone boundaries (speed, m/s), never derived from heart rate.
// A higher zone is a faster pace (a lower minutes-per-km value), so boundaries read as a
// floor speed phrased "this fast or faster", not a plain "≥ value" like heart rate/power.
const paceRows = [
  {kind:'pace',zone:0,low:0,seconds:120},
  {kind:'pace',zone:1,low:2.5,seconds:600},
  {kind:'pace',zone:2,low:3.0,seconds:900},
  {kind:'pace',zone:3,low:3.5,seconds:300},
];
const paceScale = zoneScale(paceRows,'pace');
assert.equal(paceScale.length,4);
assert.equal(zoneAt(paceScale,2.6),1);
assert.equal(zoneAt(paceScale,3.6),3);
assert.equal(zoneTitle('pace'),'Time in pace zones');
assert.equal(zoneTitle('pace','cycling'),'Time in speed zones');
assert.equal(zoneBoundaryText('pace','running',0),'');
assert.equal(zoneBoundaryText('pace','running',3.0),'5:33/km or faster');
assert.equal(zoneBoundaryText('pace','strength',3.0),''); // no pace unit for this sport
assert.equal(zoneBoundaryText('hr','running',150),'≥ 150 bpm'); // unchanged for hr/power
const paceHtml = zoneRows(paceRows,{kind:'pace',boundaries:true,sport:'running'});
assert.match(paceHtml,/5:33\/km or faster/);
assert.doesNotMatch(paceHtml,/≥/); // never the misleading "≥" direction for pace
console.log('Zone identity, boundaries, missing/invalid data and accessible labels passed');
