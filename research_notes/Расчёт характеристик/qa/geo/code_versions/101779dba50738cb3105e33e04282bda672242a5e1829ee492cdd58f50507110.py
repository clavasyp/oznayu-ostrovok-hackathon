"""Representation-only containers for the unchanged accepted Graph constructor.

No edge selection, coordinate rounding, access rule or distance algorithm changes.
OSM node IDs fit exactly in float64 for this frozen snapshot; reject larger IDs.
"""
import sys
from collections.abc import Mapping
import numpy as np

class PackedNodes:
    __slots__=('array',)
    def __init__(self,values):
        self.array=values if isinstance(values,np.ndarray) else np.asarray(values,dtype=np.float64)
        if self.array.size and np.max(np.abs(self.array[:,0]))>2**53:raise ValueError('Node ID does not fit exact float64 representation')
    def __len__(self):return len(self.array)
    def __iter__(self):
        for v in self.array:yield int(v[0]),float(v[1]),float(v[2])
    def __getitem__(self,key):
        if isinstance(key,slice):return PackedNodes(self.array[key])
        v=self.array[key];return int(v[0]),float(v[1]),float(v[2])

class SourceRecord(dict):
    """Release the source WKB string after the constructor creates its GEOS copy."""
    def __getitem__(self,key):
        value=super().__getitem__(key)
        if key=='geometry_wkb':super().__delitem__(key)
        return value

class EdgeValue(Mapping):
    __slots__=('length','metadata')
    def __init__(self,length,metadata):self.length=length;self.metadata=metadata
    def __getitem__(self,key):return self.length if key=='length_m' else self.metadata[key]
    def __iter__(self):yield 'length_m';yield from self.metadata
    def __len__(self):return len(self.metadata)+1

class SharedEdges(dict):
    def __init__(self,*a,**kw):super().__init__();self.metadata_by_record={};self.update(*a,**kw)
    def __setitem__(self,key,value):
        record_key=id(value['record']);meta=self.metadata_by_record.get(record_key)
        if meta is None:
            meta={k:v for k,v in value.items() if k!='length_m'};self.metadata_by_record[record_key]=meta
        super().__setitem__(key,EdgeValue(value['length_m'],meta))

def compact_record(record):
    r=SourceRecord((sys.intern(k),v) for k,v in record.items())
    r['tags']={sys.intern(k):sys.intern(v) if isinstance(v,str) else v for k,v in r['tags'].items()}
    if r.get('nodes'):r['nodes']=PackedNodes(r['nodes'])
    return r

def graph_class(original):
    class MemoryGraph(original):
        @property
        def edges(self):return self._shared_edges
        @edges.setter
        def edges(self,value):
            edges=SharedEdges()
            for key,v in value.items():edges[key]=v
            self._shared_edges=edges
    MemoryGraph.__name__='MemoryGraph_UnchangedConstructor'
    return MemoryGraph
