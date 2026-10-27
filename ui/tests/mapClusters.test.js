import test from 'node:test'
import assert from 'node:assert/strict'
import { groupMapPoints } from '../src/mapClusters.js'

test('dense hotel markers group without losing or duplicating hotels', () => {
  const points = [{ id: 'a', x: 10, y: 15 }, { id: 'b', x: 20, y: 25 }, { id: 'c', x: 200, y: 200 }]
  const groups = groupMapPoints(points)
  assert.deepEqual(groups.map((group) => group.length), [2, 1])
  assert.deepEqual(groups.flat().map((point) => point.id).sort(), ['a', 'b', 'c'])
  assert.equal(groupMapPoints(points.map((point) => ({ ...point, x: point.x * 10, y: point.y * 10 }))).length, 3)
  assert.deepEqual(groupMapPoints([]), [])
})

test('neighbours across a grid line share a group; at maximum zoom only identical points group', () => {
  assert.equal(groupMapPoints([{ id: 'a', x: 55, y: 10 }, { id: 'b', x: 57, y: 10 }]).length, 1)
  const same = [{ id: 'a', x: 100, y: 100 }, { id: 'b', x: 100, y: 100 }, { id: 'c', x: 104, y: 100 }]
  assert.deepEqual(groupMapPoints(same, 0).map((group) => group.map((point) => point.id)), [['a', 'b'], ['c']])
})
