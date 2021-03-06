"""
Accumulate all experiment data into a set of linked tables.
"""
import io
import numpy as np

import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, Boolean, Float, Date, DateTime, LargeBinary, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import func

from config import synphys_db_host, synphys_db


table_schemas = {
    'slice': [
        "All brain slices on which an experiment was attempted.",
        ('acq_timestamp', 'datetime', 'Creation timestamp for slice data acquisition folder.', {'unique': True}),
        ('species', 'str', 'Human | mouse (from LIMS)'),
        ('age', 'int', 'Specimen age (in days) at time of dissection (from LIMS)'),
        ('genotype', 'str', 'Specimen donor genotype (from LIMS)'),
        ('orientation', 'str', 'Orientation of the slice plane (eg "sagittal"; from LIMS specimen name)'),
        ('surface', 'str', 'The surface of the slice exposed during the experiment (eg "left"; from LIMS specimen name)'),
        ('hemisphere', 'str', 'The brain hemisphere from which the slice originated. (from LIMS specimen name)'),
        ('quality', 'int', 'Experimenter subjective slice quality assessment (0-5)'),
        ('slice_time', 'datetime', 'Time when this specimen was sliced'),
        ('slice_conditions', 'object', 'JSON containing solutions, perfusion, incubation time, etc.'),
        ('lims_specimen_name', 'str', 'Name of LIMS "slice" specimen'),
        ('original_path', 'str', 'Original path of the slice folder on the acquisition rig'),
        ('submission_data', 'object'),          # structure generated for original submission
    ],
    'experiment': [
        "A group of cells patched simultaneously in the same slice.",
        ('original_path', 'str', 'Describes original location of raw data'),
        ('acq_timestamp', 'datetime', 'Creation timestamp for site data acquisition folder.', {'unique': True}),
        ('slice_id', 'slice.id'),
        ('target_region', 'str', 'The intended brain region for this experiment'),
        ('internal', 'str', 'The name of the internal solution used in this experiment. '
                            'The solution should be described in the pycsf database.'),
        ('acsf', 'str', 'The name of the ACSF solution used in this experiment. '
                        'The solution should be described in the pycsf database.'),
        ('target_temperature', 'float'),
        ('date', 'datetime'),
        ('lims_specimen_id', 'int', 'ID of LIMS "CellCluster" specimen.'),
        ('submission_data', 'object', 'structure generated for original submission.'),
        ('lims_trigger_id', 'int', 'ID used to query status of LIMS upload.'),
        ('connectivity_analysis_complete', 'bool'),
        ('kinetics_analysis_complete', 'bool'),
    ],
    'electrode': [
        "Each electrode records a patch attempt, whether or not it resulted in a "
        "successful cell recording.",
        ('expt_id', 'experiment.id'),
        ('patch_status', 'str', 'no seal, low seal, GOhm seal, tech fail, ...'),
        ('device_key', 'int'),
        ('initial_resistance', 'float'),
        ('initial_current', 'float'),
        ('pipette_offset', 'float'),
        ('final_resistance', 'float'),
        ('final_current', 'float'),
        ('notes', 'str'),
    ],
    'cell': [
        ('electrode_id', 'electrode.id'),
        ('cre_type', 'str'),
        ('patch_start', 'float'),
        ('patch_stop', 'float'),
        ('seal_resistance', 'float'),
        ('has_biocytin', 'bool'),
        ('has_dye_fill', 'bool'),
        ('pass_qc', 'bool'),
        ('pass_spike_qc', 'bool'),
        ('depth', 'float'),
        ('position', 'object'),
    ],
    
    'pair': [
        "All possible putative synaptic connections",
        ('pre_cell', 'cell.id'),
        ('post_cell', 'cell.id'),
        ('synapse', 'bool', 'Whether the experimenter thinks there is a synapse'),
        ('electrical', 'bool', 'whether the experimenter thinks there is a gap junction'),
    ],
    'sync_rec': [
        ('experiment_id', 'experiment.id'),
        ('sync_rec_key', 'object'),
        ('temperature', 'float'),
        ('meta', 'object'),
    ],
    'recording': [
        ('sync_rec_id', 'sync_rec.id', 'References the synchronous recording to which this recording belongs.'),
        ('device_key', 'int', 'Identifies the device that generated this recording (this is usually the MIES AD channel)'),
        ('start_time', 'datetime', 'The clock time at the start of this recording'),
    ],
    'patch_clamp_recording': [
        "Extra data for recordings made with a patch clamp amplifier",
        ('recording_id', 'recording.id'),
        ('electrode_id', 'electrode.id', 'References the patch electrode that was used during this recording'),
        ('clamp_mode', 'str', 'The mode used by the patch clamp amplifier: "ic" or "vc"'),
        ('patch_mode', 'str', "The state of the membrane patch. E.g. 'whole cell', 'cell attached', 'loose seal', 'bath', 'inside out', 'outside out'"),
        ('stim_name', 'object', "The name of the stimulus protocol"),
        ('baseline_potential', 'float'),
        ('baseline_current', 'float'),
        ('baseline_rms_noise', 'float'),
        ('nearest_test_pulse_id', 'test_pulse.id'),
    ],
    'multi_patch_probe': [
        "Extra data for multipatch recordings intended to test synaptic connections.",
        ('patch_clamp_recording_id', 'patch_clamp_recording.id'),
        ('induction_frequency', 'float'),
        ('recovery_delay', 'float'),
        ('n_spikes_evoked', 'int'),
    ],
    'test_pulse': [
        ('start_index', 'int'),
        ('stop_index', 'int'),
        ('baseline_current', 'float'),
        ('baseline_potential', 'float'),
        ('access_resistance', 'float'),
        ('input_resistance', 'float'),
        ('capacitance', 'float'),
        ('time_constant', 'float'),
    ],
    'stim_pulse': [
        "A pulse stimulus intended to evoke an action potential",
        ('recording_id', 'recording.id'),
        ('pulse_number', 'int'),
        ('onset_time', 'float'),
        ('onset_index', 'int'),
        ('next_pulse_index', 'int'),      # index of the next pulse on any channel in the sync rec
        ('amplitude', 'float'),
        ('length', 'int'),
        ('n_spikes', 'int'),                           # number of spikes evoked
    ],
    'stim_spike': [
        "An evoked action potential",
        ('recording_id', 'recording.id'),
        ('pulse_id', 'stim_pulse.id'),
        ('peak_index', 'int'),
        ('peak_diff', 'float'),
        ('peak_val', 'float'),
        ('rise_index', 'int'),
        ('max_dvdt', 'float'),
    ],
    'baseline': [
        "A snippet of baseline data, matched to a postsynaptic recording",
        ('recording_id', 'recording.id', 'The recording from which this baseline snippet was extracted.'),
        ('start_index', 'int', 'start index of this snippet, relative to the beginning of the recording'),
        ('stop_index', 'int', 'stop index of this snippet, relative to the beginning of the recording'),
        ('data', 'array', 'numpy array of baseline data sampled at 20kHz'),
        ('mode', 'float', 'most common value in the baseline snippet'),
    ],
    'pulse_response': [
        "A postsynaptic recording taken during a presynaptic stimulus",
        ('recording_id', 'recording.id'),
        ('pulse_id', 'stim_pulse.id'),
        ('pair_id', 'pair.id'),
        ('start_index', 'int'),
        ('stop_index', 'int'),
        ('data', 'array', 'numpy array of response data sampled at 20kHz'),
        ('baseline_id', 'baseline.id'),
    ],
}




#----------- define ORM classes -------------

ORMBase = declarative_base()

class NDArray(TypeDecorator):
    """For marshalling arrays in/out of binary DB fields.
    """
    impl = LargeBinary
    
    def process_bind_param(self, value, dialect):
        buf = io.BytesIO()
        np.save(buf, value, allow_pickle=False)
        return buf.getvalue()
        
    def process_result_value(self, value, dialect):
        buf = io.BytesIO(value)
        return np.load(buf, allow_pickle=False)


class FloatType(TypeDecorator):
    """For marshalling float types (including numpy).
    """
    impl = Float
    
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return float(value)
        
    #def process_result_value(self, value, dialect):
        #buf = io.BytesIO(value)
        #return np.load(buf, allow_pickle=False)


_coltypes = {
    'int': Integer,
    'float': FloatType,
    'bool': Boolean,
    'str': String,
    'date': Date,
    'datetime': DateTime,
    'array': NDArray,
    'object': JSONB,
}


def _generate_mapping(table):
    """Generate an ORM mapping class from an entry in table_schemas.
    """
    name = table.capitalize()
    schema = table_schemas[table]
    table_args = {}
    if isinstance(schema[0], str):
        table_args['comment'] = schema[0]
        schema = schema[1:]
    
    props = {
        '__tablename__': table,
        '__table_args__': table_args,
        'id': Column(Integer, primary_key=True),
        'time_created': Column(DateTime, default=func.now()),
        'time_modified': Column(DateTime, onupdate=func.current_timestamp()),
        'meta': Column(JSONB),
    }
    for column in schema:
        colname, coltype = column[:2]
        kwds = {} if len(column) < 4 else column[3]
        kwds['comment'] = None if len(column) < 3 else column[2]
            
        if coltype not in _coltypes:
            if not coltype.endswith('.id'):
                raise ValueError("Unrecognized column type %s" % coltype)
            props[colname] = Column(Integer, ForeignKey(coltype), **kwds)
        else:
            ctyp = _coltypes[coltype]
            props[colname] = Column(ctyp, **kwds)
    return type(name, (ORMBase,), props)


# Generate ORM mapping classes
Slice = _generate_mapping('slice')
Experiment = _generate_mapping('experiment')
Electrode = _generate_mapping('electrode')
Cell = _generate_mapping('cell')
Pair = _generate_mapping('pair')
SyncRec = _generate_mapping('sync_rec')
Recording = _generate_mapping('recording')
PatchClampRecording = _generate_mapping('patch_clamp_recording')
MultiPatchProbe = _generate_mapping('multi_patch_probe')
TestPulse = _generate_mapping('test_pulse')
StimPulse = _generate_mapping('stim_pulse')
StimSpike = _generate_mapping('stim_spike')
PulseResponse = _generate_mapping('pulse_response')
Baseline = _generate_mapping('baseline')

# Set up relationships
Slice.experiments = relationship("Experiment", order_by=Experiment.id, back_populates="slice")
Experiment.slice = relationship("Slice", back_populates="experiments")

Experiment.sync_recs = relationship(SyncRec, order_by=SyncRec.id, back_populates="experiment", cascade='delete', single_parent=True)
SyncRec.experiment = relationship(Experiment, back_populates='sync_recs')

SyncRec.recordings = relationship(Recording, order_by=Recording.id, back_populates="sync_rec", cascade="delete", single_parent=True)
Recording.sync_rec = relationship(SyncRec, back_populates="recordings")

Recording.patch_clamp_recording = relationship(PatchClampRecording, back_populates="recording", cascade="delete", single_parent=True)
PatchClampRecording.recording = relationship(Recording, back_populates="patch_clamp_recording")

PatchClampRecording.multi_patch_probe = relationship(MultiPatchProbe, back_populates="patch_clamp_recording", cascade="delete", single_parent=True)
MultiPatchProbe.patch_clamp_recording = relationship(PatchClampRecording, back_populates="multi_patch_probe")

PatchClampRecording.nearest_test_pulse = relationship(TestPulse, cascade="delete", single_parent=True, foreign_keys=[PatchClampRecording.nearest_test_pulse_id])
#TestPulse.patch_clamp_recording = relationship(PatchClampRecording)

Recording.stim_pulses = relationship(StimPulse, back_populates="recording", cascade="delete", single_parent=True)
StimPulse.recording = relationship(Recording, back_populates="stim_pulses")

Recording.stim_spikes = relationship(StimSpike, back_populates="recording", cascade="delete", single_parent=True)
StimSpike.recording = relationship(Recording, back_populates="stim_spikes")

StimSpike.pulse = relationship(StimPulse)

Recording.baselines = relationship(Baseline, back_populates="recording", cascade="delete", single_parent=True)
Baseline.recording = relationship(Recording, back_populates="baselines")

PulseResponse.recording = relationship(Recording)
PulseResponse.stim_pulse = relationship(StimPulse)
PulseResponse.baseline = relationship(Baseline)


#-------------- initial DB access ----------------

# recreate all tables in DB
# (just for initial development)
import sys, sqlalchemy
if '--reset-db' in sys.argv:
    engine = create_engine(synphys_db_host + '/postgres')
    #ORMBase.metadata.drop_all(engine)
    conn = engine.connect()
    conn.connection.set_isolation_level(0)
    try:
        conn.execute('drop database synphys')
    except sqlalchemy.exc.ProgrammingError as err:
        if 'does not exist' not in err.message:
            raise
    conn.execute('create database synphys')
    conn.close()
    
    # connect to DB
    engine = create_engine(synphys_db_host + '/' + synphys_db)
    ORMBase.metadata.create_all(engine)
else:

    # connect to DB
    engine = create_engine(synphys_db_host + '/' + synphys_db)



# external users should create sessions from here.
Session = sessionmaker(bind=engine)





def default_session(fn):
    def wrap_with_session(*args, **kwds):
        close = False
        if kwds.get('session', None) is None:
            kwds['session'] = Session()
            close = True
        try:
            ret = fn(*args, **kwds)
            return ret
        finally:
            if close:
                kwds['session'].close()
    return wrap_with_session    


@default_session
def slice_from_timestamp(ts, session=None):
    slices = session.query(Slice).filter(Slice.acq_timestamp==ts).all()
    if len(slices) == 0:
        raise KeyError("No slice found for timestamp %s" % ts)
    elif len(slices) > 1:
        raise KeyError("Multiple slices found for timestamp %s" % ts)
    
    return slices[0]


@default_session
def experiment_from_timestamp(ts, session=None):
    expts = session.query(Experiment).filter(Experiment.acq_timestamp==ts).all()
    if len(expts) == 0:
        raise KeyError("No experiment found for timestamp %s" % ts)
    elif len(expts) > 1:
        raise RuntimeError("Multiple experiments found for timestamp %s" % ts)
    
    return expts[0]



if __name__ == '__main__':
    # start a session
    session = Session()
    
    sl = Slice(lims_specimen_name="xxxxx", surface='medial')
    exp1 = Experiment(slice=sl, acsf='MP ACSF 1')
    exp2 = Experiment(slice=sl, acsf='MP ACSF 1')
    
    srec1 = SyncRec(experiment=exp1)
    srec2 = SyncRec(experiment=exp2)
    srec3 = SyncRec(experiment=exp2)
    
    rec1 = Recording(sync_rec=srec1)
    rec2 = Recording(sync_rec=srec2)
    rec3 = Recording(sync_rec=srec3)
    rec4 = Recording(sync_rec=srec3)
    
    pcrec1 = PatchClampRecording(recording=rec1)
    pcrec2 = PatchClampRecording(recording=rec2)
    pcrec3 = PatchClampRecording(recording=rec3)
    
    tp1 = TestPulse()
    tp2 = TestPulse()
    pcrec1.nearest_test_pulse = tp1
    pcrec2.nearest_test_pulse = tp2
    
    session.add_all([sl, exp1, exp2, srec1, srec2, srec3, rec1, rec2, rec3, rec4, pcrec1, pcrec2, pcrec3, tp1, tp2])
    session.commit()
