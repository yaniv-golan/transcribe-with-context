import logging
import concurrent.futures
from typing import List, Dict, Tuple, Optional, Any
import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
from tqdm import tqdm
from yawt.config import SAMPLING_RATE
import torch.nn.functional as F
import numpy as np
from iso639 import Lang, iter_langs
from iso639.exceptions import InvalidLanguageValue

class TimeoutException(Exception):
    """
    Custom exception to indicate a timeout during transcription.
    """
    pass

def get_device() -> torch.device:
    """
    Determines the available device for computation: CUDA, MPS, or CPU.

    Returns:
        torch.device: The selected device based on availability.
    """
    if torch.cuda.is_available():
        return torch.device("cuda")
    elif torch.backends.mps.is_available():
        return torch.device("mps")
    else:
        return torch.device("cpu")

def load_and_optimize_model(model_id):
    """
    Loads and optimizes the speech-to-text model.

    Args:
        model_id (str): The identifier for the model to load.

    Returns:
        tuple: Contains the model, processor, device, and torch data type.
    """
    try:
        logging.info(f"Loading model '{model_id}'...")
        device = get_device()
        torch_dtype = torch.float16 if device.type in ["cuda", "mps"] else torch.float32

        # Load the pre-trained speech-to-text model with specified configurations
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_id,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            attn_implementation="sdpa"
        ).to(device)

        # Convert model to half precision if on CUDA or MPS for performance
        if device.type in ["cuda", "mps"] and model.dtype != torch.float16:
            model = model.half()
            logging.info("Converted model to float16.")

        # Load the corresponding processor for the model
        processor = AutoProcessor.from_pretrained(model_id)
        logging.info(f"Model loaded on {device} with dtype {model.dtype}.")

        import warnings
        # Suppress specific warnings from transformers library to reduce clutter
        warnings.filterwarnings("ignore", category=FutureWarning, module="transformers.modeling_utils")
        warnings.filterwarnings("ignore", category=UserWarning, module="transformers.models.whisper.modeling_whisper")

        # Attempt to optimize the model using torch.compile for better performance
        try:
            model = torch.compile(model, mode="reduce-overhead")
            logging.info("Model optimized with torch.compile.")
        except Exception as e:
            logging.exception(f"Model optimization failed: {e}")  # Capture stack trace for debugging

        # Remove forced_decoder_ids from model configuration if present to avoid unintended behavior
        if hasattr(model.config, 'forced_decoder_ids'):
            model.config.forced_decoder_ids = None
            logging.info("Removed forced_decoder_ids from model config.")

        return model, processor, device, torch_dtype
    except Exception as e:
        logging.exception(f"Failed to load and optimize model '{model_id}': {e}")  # Capture stack trace for debugging
        sys.exit(1)  # Exit the program if model loading fails

def model_generate_with_timeout(
    model: AutoModelForSpeechSeq2Seq,
    inputs: Dict[str, torch.Tensor],
    generate_kwargs: Dict[str, Any],
    transcription_timeout: int
) -> Any:
    """
    Generates output from the model with a timeout.

    Args:
        model: The transcription model.
        inputs: The input tensor dictionary.
        generate_kwargs: Generation keyword arguments.
        transcription_timeout: Timeout for transcription in seconds.

    Returns:
        GenerateOutput: Generated token sequences and scores.

    Raises:
        TimeoutException: If transcription exceeds the specified timeout.
    """
    def generate() -> Any:
        # Add necessary generation parameters
        adjusted_kwargs = generate_kwargs.copy()
        adjusted_kwargs['input_features'] = inputs['input_features']
        adjusted_kwargs['return_dict_in_generate'] = True  # Ensure detailed output
        adjusted_kwargs['output_scores'] = True            # Include scores
        logging.debug(f"Final generate_kwargs before generation: {adjusted_kwargs}")
        return model.generate(**adjusted_kwargs, use_cache=True)
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(generate)
        try:
            return future.result(timeout=transcription_timeout)
        except concurrent.futures.TimeoutError:
            logging.error("Transcription timed out.")
            raise TimeoutException("Transcription timed out.")

def compute_per_token_confidence(outputs: Any) -> List[float]:
    """
    Computes per-token confidence scores from model outputs.
    
    Args:
        outputs: The GenerateOutput object from model.generate().
    
    Returns:
        List of per-token confidence scores.
    """
    if hasattr(outputs, 'scores'):
        token_confidences = []
        for score in outputs.scores:
            probabilities = F.softmax(score, dim=-1)
            top_prob, _ = torch.max(probabilities, dim=-1)
            token_confidences.append(top_prob.item())
        return token_confidences
    else:
        logging.warning("Output does not contain scores. Returning full confidence.")
        # If scores are not available, return full confidence
        return [1.0] * outputs.sequences.shape[1]

def aggregate_confidence(token_confidences: List[float]) -> float:
    """
    Aggregates per-token confidence scores into an overall confidence score.
    
    Args:
        token_confidences: List of per-token confidence scores.
    
    Returns:
        The average confidence score.
    """
    if not token_confidences:
        return 0.0
    overall_confidence = sum(token_confidences) / len(token_confidences)
    return overall_confidence


# to be replaced by native call to iso639.is_valid_language_token if and when https://github.com/LBeaudoux/iso639/pull/25 is approved

# Create a set of valid language codes
valid_codes = set()
for lang in iter_langs():
    if lang.pt1:
        valid_codes.add(lang.pt1.lower())  # ISO 639-1 codes
    if lang.pt2b:
        valid_codes.add(lang.pt2b.lower())  # ISO 639-2/B codes
    if lang.pt2t:
        valid_codes.add(lang.pt2t.lower())  # ISO 639-2/T codes
    if lang.pt3:
        valid_codes.add(lang.pt3.lower())  # ISO 639-3 codes
    valid_codes.add(lang.name.lower())  # Language names (lowercase)

def is_valid_language_code(code: str) -> bool:
    return code.lower() in valid_codes

def extract_language_token(generated_ids: torch.Tensor, tokenizer: Any) -> Optional[str]:
    tokens = tokenizer.convert_ids_to_tokens(generated_ids[0])
    logging.debug(f"Generated tokens: {tokens}")
    
    for token in tokens[:5]:  # Check only the first few tokens
        if token.startswith('<|') and token.endswith('|>'):
            lang_code = token[2:-2]  # Remove '<|' and '|>'
            if is_valid_language_code(lang_code):
                return lang_code
        elif token != '<|startoftranscript|>':
            break
    
    return None

def transcribe_single_segment(
    model: AutoModelForSpeechSeq2Seq,
    processor: AutoProcessor,
    inputs: Dict[str, torch.Tensor],
    generate_kwargs: Dict[str, Any],
    idx: int,
    chunk_start: int,
    chunk_end: int,
    device: torch.device,
    torch_dtype: torch.dtype,
    transcription_timeout: int,
    max_target_positions: int,
    buffer_tokens: int,
    main_language: Optional[str] = None
) -> Tuple[Optional[str], float, Optional[str]]:
    try:
        adjusted_generate_kwargs = generate_kwargs.copy()
        if main_language:
            adjusted_generate_kwargs["language"] = main_language

        input_length = inputs['input_features'].shape[1]
        max_length = model.config.max_length if hasattr(model.config, 'max_length') else max_target_positions
        prompt_length = adjusted_generate_kwargs.get('decoder_input_ids', torch.tensor([])).shape[-1]
        max_new_tokens = max(256, max_length - input_length - prompt_length - buffer_tokens - 1)

        if max_new_tokens <= 0:
            logging.error(f"Calculated max_new_tokens is non-positive: {max_new_tokens}")
            return None, 0.0, None

        adjusted_generate_kwargs["max_new_tokens"] = min(
            adjusted_generate_kwargs.get("max_new_tokens", max_new_tokens),
            max_new_tokens
        )

        logging.debug(f"Segment {idx}: Input features shape: {inputs['input_features'].shape}")
        logging.debug(f"Segment {idx}: Generate kwargs: {adjusted_generate_kwargs}")

        if 'input_features' not in adjusted_generate_kwargs:
            adjusted_generate_kwargs['input_features'] = inputs['input_features']

        outputs = model_generate_with_timeout(
            model=model,
            inputs=inputs,
            generate_kwargs=adjusted_generate_kwargs,
            transcription_timeout=transcription_timeout  
        )

        # Debug logging for outputs
        logging.debug(f"Segment {idx}: Type of outputs: {type(outputs)}")
        logging.debug(f"Segment {idx}: Outputs attributes: {dir(outputs)}")

        # Ensure outputs have 'sequences' and 'scores'
        if hasattr(outputs, 'sequences') and hasattr(outputs, 'scores'):
            transcription = processor.batch_decode(outputs.sequences, skip_special_tokens=True)[0].strip()
            token_confidences = compute_per_token_confidence(outputs)
            overall_confidence = aggregate_confidence(token_confidences)
            language_token = extract_language_token(outputs.sequences, processor.tokenizer)
        else:
            logging.error(f"Segment {idx}: Unexpected output format")
            return None, 0.0, None

        logging.debug(f"Segment {idx}: Transcription: '{transcription}', Confidence: {overall_confidence}, Language: {language_token}")

        return transcription, overall_confidence, language_token
    except TimeoutException:
        logging.error(f"Transcription timed out for segment {idx} ({chunk_start}-{chunk_end}s)")
        return None, 0.0, None
    except Exception as e:
        logging.exception(f"Unexpected error during transcription of segment {idx}: {e}")
        return None, 0.0, None

def evaluate_confidence(
    overall_confidence: float,
    language_token: Optional[str],
    threshold: float = 0.6,
    main_language: str = 'en'  # Renamed from primary_language to main_language
) -> bool:
    if overall_confidence == 0.0:
        logging.warning(f"Zero confidence detected. Confidence: {overall_confidence}")
        return False
    
    is_high_confidence = overall_confidence >= threshold
    
    if language_token is None:
        is_main_language = False
    else:
        if not is_valid_language_code(language_token) or not is_valid_language_code(main_language):
            logging.warning(f"Unrecognized language code: {language_token} or {main_language}")
            is_main_language = False
        else:
            is_main_language = language_token.lower() == main_language.lower()
    
    logging.debug(f"Confidence evaluation: Overall confidence: {overall_confidence}, Detected Language: {language_token}, Main Language: {main_language}")
    logging.debug(f"Evaluation result: High confidence: {is_high_confidence}, Is main language: {is_main_language}")
    
    return is_high_confidence and is_main_language

def transcribe_segments(
    diarization_segments: List[Dict[str, Any]],
    audio_array: np.ndarray,
    model: AutoModelForSpeechSeq2Seq,
    processor: AutoProcessor,
    device: torch.device,
    torch_dtype: torch.dtype,
    max_target_positions: int,
    buffer_tokens: int,
    transcription_timeout: int,
    generate_kwargs: Dict[str, Any],
    confidence_threshold: float,
    main_language: str = 'en'
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    transcription_segments = []
    failed_segments = []

    for idx, segment in enumerate(tqdm(diarization_segments, desc="Transcribing Segments", unit="segment"), 1):
        try:
            chunk_start = int(segment['start'])
            chunk_end = int(segment['end'])
            chunk = audio_array[int(chunk_start * SAMPLING_RATE):int(chunk_end * SAMPLING_RATE)]
            inputs = processor(chunk, sampling_rate=SAMPLING_RATE, return_tensors="pt")
            inputs = {k: v.to(device).to(torch_dtype) for k, v in inputs.items()}
            inputs['attention_mask'] = torch.ones_like(inputs['input_features'])

            transcription, overall_confidence, language_token = transcribe_single_segment(
                model=model,
                processor=processor,
                inputs=inputs,
                generate_kwargs=generate_kwargs,
                idx=idx,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                device=device,
                torch_dtype=torch_dtype,
                transcription_timeout=transcription_timeout,
                max_target_positions=max_target_positions,
                buffer_tokens=buffer_tokens,
                main_language=main_language
            )

            transcript = {
                'speaker_id': segment['speaker_id'],
                'start': segment['start'],
                'end': segment['end'],
                'text': transcription if transcription else "",
                'confidence': overall_confidence,
                'language': language_token,
                'low_confidence': overall_confidence < confidence_threshold or not evaluate_confidence(
                    overall_confidence, language_token, threshold=confidence_threshold, main_language=main_language
                )
            }
            transcription_segments.append(transcript)

            logging.debug(f"Segment {idx}: Transcription result - Text: '{transcription}', Confidence: {overall_confidence}, Language: {language_token}")

            if transcript['low_confidence']:
                failed_segments.append({
                    'segment_index': idx,
                    'segment': segment,
                    'transcription': transcription,
                    'confidence': overall_confidence,
                    'language': language_token,
                    'reason': f'Low confidence transcription ({overall_confidence:.2f}) or incorrect language detection.'
                })
                logging.warning(f"Low confidence or incorrect language transcription for segment {idx} ({segment['start']}-{segment['end']}s). Transcription: '{transcription}', Confidence: {overall_confidence:.2f}, Detected Language: {language_token}")
        except Exception as e:
            failed_segments.append({'segment_index': idx, 'segment': segment, 'reason': str(e)})
            logging.exception(f"Failed to transcribe segment {idx} ({segment}): {e}")

    return transcription_segments, failed_segments

def retry_transcriptions(
    model: AutoModelForSpeechSeq2Seq,
    processor: AutoProcessor,
    audio_array: np.ndarray,
    diarization_segments: List[Dict[str, Any]],
    failed_segments: List[Dict[str, Any]],
    generate_kwargs: Dict[str, Any],
    device: torch.device,
    torch_dtype: torch.dtype,
    base_name: str,
    transcription_segments: List[Dict[str, Any]],
    max_target_positions: int,
    buffer_tokens: int,
    transcription_timeout: int,
    secondary_language: Optional[str] = None,
    confidence_threshold: float = 0.6,
    main_language: str = 'en',
    max_retries: int = 3
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if secondary_language and is_valid_language_code(secondary_language):
        lang = secondary_language.lower()[:2]  # Use ISO 639-1 (2-letter) code
    else:
        if secondary_language:
            logging.warning(f"Invalid secondary language code: {secondary_language}")
        return transcription_segments, failed_segments  # No valid secondary language to retry with

    for attempt in range(1, max_retries + 1):
        if not failed_segments:
            break
        logging.info(f"Starting retry attempt {attempt} for failed segments with secondary language '{lang}'.")
        logging.info(f"Number of segments to retry: {len(failed_segments)}")

        retry_failed_segments = []
        for failure in tqdm(failed_segments, desc=f"Retrying Segments (Attempt {attempt})", unit="segment"):
            idx = failure['segment_index']
            seg = diarization_segments[idx - 1]
            start, end = seg['start'], seg['end']
            logging.info(f"Retrying transcription for segment {idx}: {start}-{end}s")

            try:
                # Adjust generate_kwargs for secondary language
                current_generate_kwargs = generate_kwargs.copy()
                current_generate_kwargs["language"] = lang

                chunk_start = int(start)
                chunk_end = int(end)
                chunk = audio_array[int(chunk_start * SAMPLING_RATE):int(chunk_end * SAMPLING_RATE)]
                inputs = processor(chunk, sampling_rate=SAMPLING_RATE, return_tensors="pt")
                inputs = {k: v.to(device).to(torch_dtype) for k, v in inputs.items()}
                inputs['attention_mask'] = torch.ones_like(inputs['input_features'])

                transcription, overall_confidence, language_token = transcribe_single_segment(
                    model=model,
                    processor=processor,
                    inputs=inputs,
                    generate_kwargs=current_generate_kwargs,
                    idx=idx,
                    chunk_start=chunk_start,
                    chunk_end=chunk_end,
                    device=device,
                    torch_dtype=torch_dtype,
                    transcription_timeout=transcription_timeout,
                    max_target_positions=max_target_positions,
                    buffer_tokens=buffer_tokens,
                    main_language=lang  # Use secondary language as main_language in retries
                )

                if transcription and evaluate_confidence(overall_confidence, language_token, threshold=confidence_threshold, main_language=lang):
                    # Update the existing transcription segment
                    for t_seg in transcription_segments:
                        if (t_seg['speaker_id'] == seg['speaker_id'] and 
                            t_seg['start'] == start and 
                            t_seg['end'] == end):
                            t_seg['text'] = transcription
                            t_seg['confidence'] = overall_confidence
                            t_seg['language'] = language_token
                            break
                else:
                    logging.warning(f"Retry {attempt}: Failed to transcribe segment {idx} with sufficient confidence.")
                    retry_failed_segments.append(failure)
            except Exception as e:
                logging.exception(f"Retry attempt {attempt} for segment {idx} failed: {e}")
                retry_failed_segments.append(failure)

        failed_segments = retry_failed_segments
        logging.info(f"After retry attempt {attempt}, {len(failed_segments)} segments still failed.")

    return transcription_segments, failed_segments  # Return both lists
