import re
from collections import Counter
from typing import List, Tuple

# Pilihan model HF multilingual yang mampu menangani Bahasa Indonesia dan English.
HF_MODEL_NAME = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
LABEL_MAP = {
    'LABEL_0': 'negative',
    'LABEL_1': 'neutral',
    'LABEL_2': 'positive',
}

TRANSFORMER_AVAILABLE = False
try:
    from transformers import pipeline,  logging as hf_logging
    hf_logging.set_verbosity_error()
    TRANSFORMER_AVAILABLE = True
except Exception:
    TRANSFORMER_AVAILABLE = False

POSITIVE_SCORE_2 = [
    'bagus', 'keren', 'mantap', 'luar biasa', 'puas', 'suka', 'oke banget', 'sangat bagus',
    'inspiratif', 'bermanfaat', 'menarik', 'seru', 'menyenangkan', 'interaktif',
    'informatif', 'membantu', 'memuaskan', 'bagus sekali', 'sangat baik'
]
POSITIVE_SCORE_1 = [
    'baik', 'cukup bagus', 'lumayan bagus',
    'cukup baik', 'cukup menarik', 'lumayan', 'cukup membantu', 'oke'
]

NEGATIVE_SCORE_2 = [
    'jelek', 'buruk', 'mengecewakan', 'kacau',
    'lambat', 'error', 'rusak', 'gagal', 'sulit', 'kesulitan',
    'bermasalah', 'tidak berfungsi', 'bug', 'crash', 'lemot',
    'loading lama', 'gangguan', 'tidak bisa', 'susah digunakan',
    'membosankan', 'kurang menarik', 'tidak jelas', 'membingungkan',
    'tidak efektif', 'kurang bermanfaat', 'tidak sesuai', 'terlalu lama',
    'tidak terorganisir', 'kurang interaktif', 'monoton'
]
NEGATIVE_SCORE_1 = [
    'kurang', 'tidak bagus', 'tidak puas',
    'kurang cepat', 'kurang stabil', 'agak lambat',
    'sedikit lambat', 'kadang error', 'kadang gagal',
    'kurang jelas', 'agak membosankan', 'kurang efektif',
    'biasa saja', 'kurang maksimal', 'kurang baik'
]

NEUTRAL_ANCHORS = [
    'biasa saja', 'standar', 'tidak ada yang terlalu menarik',
    'tidak terlalu menarik', 'sesuai jadwal', 'normal', 'seperti biasa',
    'cukup baik', 'lumayan', 'sesuai ekspektasi'
]

QUESTION_INDICATORS = [
    'apakah ada', 'apakah', 'bagaimana', 'kapan', 'dimana', 'di mana',
    'siapa', 'kenapa', 'mengapa', 'bisakah', 'adakah'
]

EXPLICIT_OPINIONS = [
    'saya merasa', 'menurut saya', 'saya suka', 'saya tidak suka',
    'menurutku', 'menurut saya acara ini', 'acara ini', 'event ini',
    'website ini', 'program ini'
]

IMPROVEMENT_PHRASES = [
    'perlu peningkatan', 'perlu ditingkatkan', 'bisa lebih baik',
    'masih bisa ditingkatkan', 'perlu diperbaiki', 'ada yang perlu diperbaiki',
    'perlu pengembangan', 'bisa lebih maksimal', 'masih kurang optimal'
]

STOPWORDS = set([
    'yang', 'dan', 'di', 'ke', 'dari', 'untuk', 'ini', 'itu', 'adalah',
    'saya', 'kamu', 'dia', 'kita', 'kami', 'dengan', 'seperti', 'bahwa',
    'atau', 'juga', 'akan', 'pada', 'tidak', 'tdk', 'yg', 'apa', 'jadi',
    'dgn', 'aja', 'sih', 'ya', 'ga', 'gak', 'juga', 'kami', 'sangat', 'lebih',
    'kurang', 'lagi', 'udah', 'sudah', 'belum', 'lagi', 'sebenernya', 'sih',
])


class SentimentAnalyzer:
    def __init__(self):
        self.pipeline = self._load_transformer_pipeline()

    def _load_transformer_pipeline(self):
        if not TRANSFORMER_AVAILABLE:
            return None

        try:
            return pipeline(
                'sentiment-analysis',
                model=HF_MODEL_NAME,
                tokenizer=HF_MODEL_NAME,
                top_k=1,
            )
        except Exception:
            return None

    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ''
        text = text.lower().strip()
        text = re.sub(r'https?://\S+|www\.\S+', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[^\w\s]', ' ', text)
        text = re.sub(r'(.)\1{2,}', r'\1', text)
        text = re.sub(r'\s+', ' ', text)
        text = text.replace('gak', 'tidak').replace('tdk', 'tidak')
        text = text.replace('terimakasih', 'terima kasih')
        text = text.replace('makasih', 'terima kasih')
        text = text.replace('tidak mengecewakan', 'oke')
        text = text.replace('tidak ada hal yang terlalu menarik atau mengecewakan', 'biasa saja')
        text = re.sub(r'\boke+\b', 'oke', text)
        text = re.sub(r'\bok+\b', 'ok', text)
        return text.strip()

    @staticmethod
    def _text_contains_any(text: str, phrases: List[str]) -> bool:
        return any(phrase in text for phrase in phrases)

    @staticmethod
    def _text_starts_with_any(text: str, phrases: List[str]) -> bool:
        text_lower = text.lower().strip()
        return any(text_lower.startswith(phrase) for phrase in phrases)

    @staticmethod
    def _rating_to_score(rating: int) -> float:
        if rating == 5: return 1.0
        elif rating == 4: return 0.5
        elif rating == 3: return 0.0
        elif rating == 2: return -0.5
        elif rating == 1: return -1.0
        return 0.0

    def predict(self, text: str, rating: int = None) -> Tuple[str, float]:
        # 1. Preprocessing
        cleaned = self.normalize_text(text)
        if not cleaned:
            return 'neutral', 0.50

        # 2. ML Baseline
        ml_sentiment = 'neutral'
        ml_score = 0.50

        if self.pipeline is not None:
            try:
                output = self.pipeline(cleaned, truncation=True, top_k=1)
                if output and isinstance(output, list):
                    result = output[0]
                    label = result.get('label', '')
                    ml_score = float(result.get('score', 0.0))
                    ml_sentiment = LABEL_MAP.get(label, 'neutral')
            except Exception:
                pass

        # 3. Keyword Scoring System
        pos_score = 0
        neg_score = 0

        for word in POSITIVE_SCORE_2:
            if word in cleaned: pos_score += 2
        for word in POSITIVE_SCORE_1:
            if word in cleaned: pos_score += 1

        for word in NEGATIVE_SCORE_2:
            if word in cleaned: neg_score += 2
        for word in NEGATIVE_SCORE_1:
            if word in cleaned: neg_score += 1
            
        priority_negatives = [
            'tidak berfungsi', 'kesulitan', 'tidak bisa', 'gagal', 'error',
            'tidak sesuai harapan', 'kurang menarik', 'membosankan', 'tidak jelas',
            'kurang efektif', 'terlalu lama'
        ]
        for word in priority_negatives:
            if word in cleaned:
                neg_score += 2

        rule_score = pos_score - neg_score

        # 4. Neutral Anchor Detection
        has_neutral_anchor = self._text_contains_any(cleaned, NEUTRAL_ANCHORS)

        # 5. Mixed Sentiment & Final Decision
        final_sentiment = 'neutral'
        is_strong_rule = False

        if rule_score >= 2:
            final_sentiment = 'positive'
            is_strong_rule = True
        elif rule_score <= -2:
            final_sentiment = 'negative'
            is_strong_rule = True
        else:
            # Score between -1 and +1
            if has_neutral_anchor:
                final_sentiment = 'neutral'
                is_strong_rule = True  # Anchor dictates strong neutral
            else:
                # Balanced / Mixed handling
                if pos_score > neg_score:
                    final_sentiment = 'positive'
                elif neg_score > pos_score:
                    final_sentiment = 'negative'
                else:
                    final_sentiment = ml_sentiment

        # 6. Confidence Balancing
        confidence = ml_score
        
        # Anggap ML setuju jika dia hanya ragu (neutral) tapi rule sangat kuat
        if is_strong_rule and ml_sentiment == 'neutral':
            ml_sentiment = final_sentiment
            
        if has_neutral_anchor and final_sentiment == 'neutral':
            confidence = max(0.90, min(1.0, ml_score + 0.45))
        elif is_strong_rule:
            if final_sentiment == ml_sentiment:
                confidence = max(0.85, min(0.99, ml_score + 0.40))
            else:
                confidence = max(0.60, min(0.84, ml_score - 0.15))
        else:
            confidence = max(0.50, min(0.70, ml_score + 0.10))

        text_lower = text.lower().strip()
        is_question_first = self._text_starts_with_any(text_lower, QUESTION_INDICATORS)
        has_explicit_opinion = self._text_contains_any(text_lower, EXPLICIT_OPINIONS)
        
        is_pure_question = is_question_first and not has_explicit_opinion
        has_improvement_phrase = self._text_contains_any(text_lower, IMPROVEMENT_PHRASES)

        if is_pure_question:
            final_sentiment = 'neutral'
            confidence = 0.50
        elif has_improvement_phrase:
            final_sentiment = 'neutral'
            confidence = 0.53

        if rating is not None and not is_pure_question:
            text_sentiment = final_sentiment
            if text_sentiment == 'positive':
                text_score = confidence
            elif text_sentiment == 'negative':
                text_score = -confidence
            else:
                text_score = 0.0
                
            rating_score = self._rating_to_score(rating)
            final_score = (text_score * 0.6) + (rating_score * 0.4)
            
            if final_score > 0.2:
                final_sentiment = 'positive'
            elif final_score < -0.2:
                final_sentiment = 'negative'
            else:
                final_sentiment = 'neutral'
                
            if final_sentiment == 'neutral':
                confidence = 0.50
            else:
                confidence = abs(final_score)
                if (text_sentiment == 'positive' and rating in [1, 2]) or \
                   (text_sentiment == 'negative' and rating in [4, 5]):
                    confidence *= 0.8

        return final_sentiment, confidence

    @staticmethod
    def extract_common_words(messages: List[str], limit: int = 10) -> List[Tuple[str, int]]:
        counter = Counter()
        for message in messages:
            cleaned = SentimentAnalyzer.normalize_text(message)
            for word in cleaned.split():
                if len(word) < 3 or word in STOPWORDS:
                    continue
                counter[word] += 1
        return counter.most_common(limit)


sentiment_analyzer = SentimentAnalyzer()
